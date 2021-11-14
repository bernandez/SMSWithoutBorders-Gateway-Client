#!/usr/bin/env python3

import time 
import os
import pika
import socket
import threading
import traceback
import json
import requests
import logging
from datetime import datetime
from base64 import b64encode

from deku import Deku
from mmcli_python.modem import Modem
from enum import Enum

from common.CustomConfigParser.customconfigparser import CustomConfigParser
from router import Router

class Gateway:

    def __init__(self, modem_index, modem_isp, config, 
            config_isp_default, config_isp_operators, ssl=None):

        self.modem_index = modem_index
        self.modem_isp = modem_isp
        self.config = config

        formatter = logging.Formatter('%(asctime)s|[%(levelname)s] [%(name)s] %(message)s', 
                datefmt='%Y-%m-%d %H:%M:%S')
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)

        logger_name=f"{self.modem_isp}:{self.modem_index}"
        self.logging=logging.getLogger(logger_name)
        self.logging.setLevel(logging.INFO)
        self.logging.addHandler(handler)

        handler = logging.FileHandler('src/services/logs/service.log')
        handler.setFormatter(formatter)
        self.logging.addHandler(handler)

        self.logging.propagate = False
        self.sleep_time = int(self.config['MODEMS']['sleep_time'])


    def monitor_incoming(self):
        connection_url=self.config['GATEWAY']['connection_url']
        queue_name=self.config['GATEWAY']['routing_queue_name']

        while(Deku.modem_ready(self.modem_index)):
            messages=Modem(self.modem_index).SMS.list('received')

            publish_connection, publish_channel = create_channel(
                    connection_url=connection_url,
                    queue_name=queue_name,
                    blocked_connection_timeout=300,
                    durable=True)

            for msg_index in messages:
                sms=Modem.SMS(index=msg_index)

                try:
                    publish_channel.basic_publish(
                            exchange='',
                            routing_key=queue_name,
                            body=json.dumps({"text":sms.text, "phonenumber":sms.number}),
                            properties=pika.BasicProperties(
                                delivery_mode=2))
                except Exception as error:
                    # self.logging.error(traceback.format_exc())
                    raise(error)
                else:
                    try:
                        Modem(self.modem_index).SMS.delete(msg_index)
                    except Exception as error:
                        self.logging.error(traceback.format_exc())

            messages=[]

            time.sleep(self.sleep_time)
        self.logging.warning("disconnected") 

        if self.modem_index in active_threads:
            del active_threads[self.modem_index]

def init_nodes(indexes, config, config_isp_default, config_isp_operators, config_event_rules):
    isp_country = config['ISP']['country']
    priority_offline_isp=config['ROUTER']['isp']

    deku=Deku(config=config, 
            config_isp_default=config_isp_default, 
            config_isp_operators=config_isp_operators)

    for modem_index in indexes:
        if modem_index not in active_nodes:
            if not deku.modem_ready(modem_index):
                continue
            try:
                modem_isp = deku.ISP.modems(
                        operator_code=Modem(modem_index).operator_code, 
                        country=isp_country)

                gateway=Gateway(modem_index, modem_isp, config, 
                        config_isp_default, config_isp_operators)

                gateway_thread=threading.Thread(
                        target=gateway.monitor_incoming, daemon=True)

                active_nodes[modem_index] = [gateway_thread, gateway]

            except Exception as error:
                raise(error)

def start_nodes():
    for modem_index, thread_n_node in active_nodes.items():
        thread = thread_n_node[0]
        try:
            if thread.native_id is None:
                thread.start()

        except Exception as error:
            raise(error)

def manage_modems(config, config_event_rules, config_isp_default, config_isp_operators):
    global active_nodes
    global sleep_time

    sleep_time = int(config['MODEMS']['sleep_time']) if \
            int(config['MODEMS']['sleep_time']) > 3 else 3

    active_nodes = {}

    logging.info('modem manager started')
    while True:
        indexes=[]
        try:
            indexes=Deku.modems_ready(remove_lock=True)
            if len(indexes) < 1:
                stdout_logging.info("No modem available")
                time.sleep(sleep_time)
                continue

        except Exception as error:
            raise(error)
        
        try:
            init_nodes(indexes, config, config_isp_default, config_isp_operators, config_event_rules)
            start_nodes()
        except Exception as error:
            raise(error)
        time.sleep(sleep_time)


def route_online(data):
    results = router.route_online(data=data)
    print(f"Routing results (ONLINE): {results.text} {results.status_code}")

def route_offline(text, number):
    results = router.route_offline(text=text, number=number)
    print("* Routing results (OFFLINE) SMS successfully routed...")

def sms_routing_callback(ch, method, properties, body):
    json_body = json.loads(body.decode('unicode_escape'))
    print(f'routing: {json_body}')

    if not "text" in json_body:
        logging.error('poorly formed message - text missing')
        routing_consume_channel.basic_ack(delivery_tag=method.delivery_tag)
        return
    if not "phonenumber" in json_body:
        logging.error('poorly formed message - number missing')
        routing_consume_channel.basic_ack(delivery_tag=method.delivery_tag)
        return

    try:
        json_data = json.dumps(json_body)
        body = str(b64encode(body), 'unicode_escape')

        if router_mode == Router.Modes.ONLINE.value:
            route_online(json_data)
            routing_consume_channel.basic_ack(delivery_tag=method.delivery_tag)

        elif router_mode == Router.Modes.OFFLINE.value:
            route_offline(body, router_phonenumber)
            routing_consume_channel.basic_ack(delivery_tag=method.delivery_tag)

        elif router_mode == Router.Modes.SWITCH.value:
            try:
                route_online(json_data)
                routing_consume_channel.basic_ack(delivery_tag=method.delivery_tag)

            except Exception as error:
                try:
                    route_offline(body, router_phonenumber)
                    routing_consume_channel.basic_ack(delivery_tag=method.delivery_tag)
                except Exception as error:
                    raise(error)
        else:
            logging.error("invalid routing protocol")
    except Exception as error:
        logging.error(traceback.format_exc())
        routing_consume_channel.basic_reject( delivery_tag=method.delivery_tag, requeue=True)
    finally:
        routing_consume_connection.sleep(sleep_time)

def create_channel(connection_url, queue_name, exchange_name=None, 
        exchange_type=None, durable=False, binding_key=None, callback=None, 
        prefetch_count=0, connection_port=5672, heartbeat=600, 
        blocked_connection_timeout=None):

    credentials=None
    try:
        parameters=pika.ConnectionParameters(
                connection_url, 
                connection_port, 
                '/', 
                heartbeat=heartbeat)

        connection=pika.BlockingConnection(parameters=parameters)
        channel=connection.channel()
        channel.queue_declare(queue_name, durable=durable)
        channel.basic_qos(prefetch_count=prefetch_count)

        if binding_key is not None:
            channel.queue_bind(
                    exchange=exchange_name,
                    queue=queue_name,
                    routing_key=binding_key)

        if callback is not None:
            channel.basic_consume(
                    queue=queue_name,
                    on_message_callback=callback)

        return connection, channel
    except Exception as error:
        raise(error)


def rabbitmq_connection(config):
    global routing_consume_connection
    global routing_consume_channel

    connection_url=config['GATEWAY']['connection_url']
    queue_name=config['GATEWAY']['routing_queue_name']

    try:
        routing_consume_connection, routing_consume_channel = create_channel(
                connection_url=connection_url,
                callback=sms_routing_callback,
                durable=True,
                prefetch_count=1,
                queue_name=queue_name)

    except Exception as error:
        raise(error)

def start_consuming():
    try:
        routing_consume_channel.start_consuming() #blocking
    except Exception as error:
        logging.error(traceback.format_exc())

def main(config, config_event_rules, config_isp_default, config_isp_operators):
    logging.info("starting gateway")

    global router_mode
    global router_phonenumber
    global router


    router_mode = config['GATEWAY']['route_mode']
    router_phonenumber = config['ROUTER']['router_phonenumber']

    url = config['ROUTER']['default']
    priority_offline_isp = config['ROUTER']['isp']
    router = Router(url=url, priority_offline_isp=priority_offline_isp, 
            config=config, config_isp_default=config_isp_default, 
            config_isp_operators=config_isp_operators)

    try:
        rabbitmq_connection(config)
        thread_rabbitmq_connection = threading.Thread(
                target=routing_consume_channel.start_consuming, daemon=True)
        thread_rabbitmq_connection.start()
    except Exception as error:
        logging.critical(traceback.format_exc())
    else:
        try:
            manage_modems(config, config_event_rules, config_isp_default, config_isp_operators)
            thread_rabbitmq_connection.join()
        except Exception as error:
            logging.error(traceback.format_exc())

if __name__ == "__main__":
    main()
