#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this
# software and associated documentation files (the "Software"), to deal in the Software
# without restriction, including without limitation the rights to use, copy, modify,
# merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
# PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#
from datetime import datetime

import boto3
import json
import logging
import os
import time

import requests

CONFIG_NAME_PREFIX = "Lacework-Control-Tower-Config-Member-"

LOGLEVEL = os.environ.get('LOGLEVEL', logging.INFO)
logger = logging.getLogger()
logger.setLevel(LOGLEVEL)
logging.getLogger("boto3").setLevel(logging.CRITICAL)
logging.getLogger("botocore").setLevel(logging.CRITICAL)
session = boto3.Session()


def lambda_handler(event, context):
    logger.info("account.lambda_handler called.")
    logger.info(json.dumps(event))
    try:
        # called from stack_setSNS
        if 'Records' in event:
            stack_set_sns_processing(event['Records'])
        # called from event bridge rule
        elif 'detail' in event and event['detail']['eventName'] == 'CreateManagedAccount':
            lifecycle_eventbridge_processing(event)
        else:
            logger.info("Event not processed.")
    except Exception as e:
        logger.error(e)


def stack_set_sns_processing(messages):
    logger.info("account.message_processing called.")
    for message in messages:
        payload = json.loads(message['Sns']['Message'])
        cfn_stack_set_processing(payload)


def lifecycle_eventbridge_processing(event):
    logger.info("account.lifecycle_processing called.")
    logger.info(json.dumps(event))
    if event['detail']['serviceEventDetails']['createManagedAccountStatus']['state'] == "SUCCEEDED":
        cloud_formation_client = session.client("cloudformation")
        account_id = event['detail']['serviceEventDetails']['createManagedAccountStatus']['account']['accountId']
        region = event['detail']['awsRegion']
        lacework_url = os.environ['lacework_url']
        lacework_account_name = get_account_from_url(lacework_url)
        lacework_sub_account_name = os.environ['lacework_sub_account_name']
        send_honeycomb_event(lacework_account_name, "add account", lacework_sub_account_name)
        config_stack_set_name = CONFIG_NAME_PREFIX + \
                                (lacework_account_name if not lacework_sub_account_name else lacework_sub_account_name)
        stack_set_instances = list_stack_instance_by_account_region(session, config_stack_set_name, account_id, region)

        logger.info("Processing Lifecycle event for {} in ".format(account_id, region))
        # stack_set instance does not exist, create a new one
        if len(stack_set_instances) == 0:
            logger.info("Create new stack set instance for {} {} {}".format(config_stack_set_name, account_id,
                                                                            region))
            message_body = {config_stack_set_name: {"target_accounts": [account_id],
                                                    "target_regions": [region]}}
            cfn_stack_set_processing(message_body)

        # stack_set instance already exist, check for missing region
        elif len(stack_set_instances) > 0:
            logger.info("Stack set instance already exists {} {} {}".format(config_stack_set_name, account_id,
                                                                            region))
    else:
        logger.error("Invalid event state, expected: SUCCEEDED : {}".format(event))


def cfn_stack_set_processing(messages):
    logger.info("account.stack_set_processing called.")
    cloud_formation_client = session.client("cloudformation")
    sns_client = session.client("sns")
    lacework_account_sns = os.environ['lacework_account_sns']
    lacework_api_credentials = os.environ['lacework_api_credentials']
    access_token = get_access_token(lacework_api_credentials)

    if access_token is None:
        logger.error("Unable to get Lacework access token. Failed to create stack instances.")
        return None

    for config_stack_set_name, params in messages.items():
        logger.info("Processing stack instances for {}".format(config_stack_set_name))
        param_accounts = params['target_accounts']
        param_regions = params['target_regions']
        logger.info("Target accounts : {}".format(param_accounts))
        logger.info("Target regions: {}".format(param_regions))

        try:
            stack_operations = True
            cloud_formation_client.describe_stack_set(StackSetName=config_stack_set_name)
            cloud_formation_paginator = cloud_formation_client.get_paginator("list_stack_set_operations")
            stack_set_iterator = cloud_formation_paginator.paginate(
                StackSetName=config_stack_set_name
            )
            for page in stack_set_iterator:
                if "Summaries" in page:
                    for operation in page['Summaries']:
                        if operation['Status'] in ("RUNNING", "STOPPING"):
                            stack_operations = False
                            break
                    if not stack_operations:
                        break

            if stack_operations:
                response = cloud_formation_client.create_stack_instances(StackSetName=config_stack_set_name,
                                                                         Accounts=param_accounts,
                                                                         Regions=param_regions,
                                                                         ParameterOverrides=[
                                                                             {
                                                                                 "ParameterKey": "AccessToken",
                                                                                 "ParameterValue": access_token,
                                                                                 "UsePreviousValue": False,
                                                                                 "ResolvedValue": "string"
                                                                             }
                                                                         ],
                                                                         OperationPreferences={
                                                                             'RegionConcurrencyType': "PARALLEL",
                                                                             'FailureToleranceCount': 999
                                                                         })

                logger.info("stack_set instance created {}".format(response))
            else:
                logger.warning("Existing stack_set operations still running")
                message_body = {config_stack_set_name: messages[config_stack_set_name]}
                try:
                    logger.info("Sleep and wait for 20 seconds")
                    time.sleep(20)
                    sns_response = sns_client.publish(
                        TopicArn=lacework_account_sns,
                        Message=json.dumps(message_body))

                    logger.info("Re-queued for stack_set instance creation: {}".format(sns_response))
                except Exception as sns_exception:
                    logger.error("Failed to send queue for stack_set instance creation: {}".format(sns_exception))

        except cloud_formation_client.exceptions.stack_setNotFoundException as describe_exception:
            logger.error("Exception getting stack set, {}".format(describe_exception))
            raise describe_exception


def list_stack_instance_by_account_region(target_session, config_stack_set_name, account_id, region):
    logger.info("account.list_stack_instance_by_account_region called.")
    logger.info(target_session)
    try:
        cfn_client = target_session.client("cloudformation")
        stack_set_result = cfn_client.list_stack_instances(
            StackSetName=config_stack_set_name,
            StackInstanceAccount=account_id,
            StackInstanceRegion=region
        )

        logger.info("stack_set_result: {}".format(stack_set_result))
        if stack_set_result and "Summaries" in stack_set_result:
            stack_set_list = stack_set_result['Summaries']
            while "NextToken" in stack_set_result:
                stack_set_result = cfn_client.list_stack_set_instance(
                    NextToken=stack_set_result['NextToken']
                )
                stack_set_list.append(stack_set_result['Summaries'])

            return stack_set_list
        else:
            return False
    except Exception as e:
        logger.error("List Stack Instance error: {}.".format(e))
        return False


def get_access_token(lacework_api_credentials):
    logger.info("account.get_access_token called.")

    secret_client = session.client('secretsmanager')
    try:
        secret_response = secret_client.get_secret_value(
            SecretId=lacework_api_credentials
        )
        if 'SecretString' not in secret_response:
            logger.error("SecretString not found in {}".format(lacework_api_credentials))
            return None

        secret_string_dict = json.loads(secret_response['SecretString'])
        access_token = secret_string_dict['AccessToken']

        return access_token

    except Exception as e:
        logger.error("Get access token error: {}.".format(e))
        return None


def get_account_from_url(lacework_url):
    return lacework_url.split('.')[0]


def send_honeycomb_event(account, event, subaccount="000000", eventdata="{}"):
    logger.info("account.send_honeycomb_event called.")

    try:
        payload = '''
        {{
            "account": "{}",
            "sub-account": "{}",
            "tech-partner": "AWS",
            "integration-name": "lacework-aws-control-tower-cloudformation",
            "version": "$BUILD",
            "service": "AWS Control Tower",
            "install-method": "cloudformation",
            "function": "account.py",
            "event": "{}",
            "event-data": {}
        }}
        '''.format(account, subaccount, event, eventdata)
        logger.info('Generate payload : {}'.format(payload))
        resp = requests.post("https://api.honeycomb.io/1/events/$DATASET",
                             headers={'X-Honeycomb-Team': '$HONEY_KEY',
                                      'content-type': 'application/json'},
                             verify=True, data=payload)
        logger.info ("Honeycomb response {} {}".format(resp, resp.content))

    except Exception as e:
        logger.warning("Get error sending to Honeycomb: {}.".format(e))
