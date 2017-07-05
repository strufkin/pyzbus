import gevent
from gevent.monkey import patch_all; patch_all()
from gevent.queue import Queue
from gevent.event import Event
import json
import logging
import setproctitle
import signal
import sys
import time
import uuid
import zmq.green as zmq


# Decorators
def check_reply(func):
    def wrapper(agent, msg, *args, **kwargs):
        res = func(agent, msg, *args, **kwargs)
        if msg.get('ReplyRequest'):
            if not res:
                res = {}
            reply = {
                # No 'To' header here as reply is returned by ask()
                'Message': '{}Reply'.format(msg.get('Message')),
                'ReplyToId': msg['Id']
            }
            reply.update(res) # Update reply with results from func.
            agent.tell(reply)
        return res
    return wrapper



class ZActor(object):
    uid = None
    greenlets = []

    receive_message_count = 0
    sent_message_count = 0
    last_msg_time = time.time()
    last_msg_time_sum = 0

    pub_socket = sub_socket = req_socket = None

    settings = {
        'UID': False,
        'SubAddr': 'tcp://127.0.0.1:8881',
        'PubAddr': 'tcp://127.0.0.1:8882',
        'ReqAddr': 'tcp://127.0.0.1:8883',
        'PingInterval': 0,
        'IdleTimeout': 200,
        'Trace': True,
        'Debug': True,
    }

    def __init__(self, settings={}):
        self.logger = self.get_logger()
        # Update startup settings
        self.load_settings(settings)

        uid = self.settings.get('UID')
        if uid:
            try:
                #int(uid) # Check that it is integer as zmq IDENTITY must be digits.
                self.uid = str(uid)
            except ValueError:
                self.logger.error('UID must be integer, not using {}'.format(uid))
                self.uid = str(uuid.getnode())
        else:
            self.uid = str(uuid.getnode())
        self.logger.info('UID: {}.'.format(self.uid))

        self.context = zmq.Context()

        self.req_socket = self.context.socket(zmq.REQ)
        self.req_socket.connect(self.settings.get('ReqAddr'))

        self.pub_socket = self.context.socket(zmq.PUB)
        self.pub_socket.connect(self.settings.get('PubAddr'))

        self.sub_socket = self.context.socket(zmq.SUB)
        self.sub_socket.connect(self.settings.get('SubAddr'))

        self.sub_socket.setsockopt(zmq.IDENTITY, self.uid)
        self.pub_socket.setsockopt(zmq.IDENTITY, self.uid)
        # Subscribe to messages for actor and also broadcasts
        self.sub_socket.setsockopt(zmq.SUBSCRIBE, b'|{}|'.format(self.uid))
        self.sub_socket.setsockopt(zmq.SUBSCRIBE, b'|*|')
        # gevent.spawn Greenlets
        self.greenlets.append(gevent.spawn(self.check_idle))
        self.greenlets.append(gevent.spawn(self.receive))
        self.greenlets.append(gevent.spawn(self.ping))
        # Install signal handler
        gevent.signal(signal.SIGINT, self.stop)
        gevent.signal(signal.SIGTERM, self.stop)


    def save_settings(self):
        try:
            with open('settings.cache', 'w') as file:
                file.write('{}\n'.format(
                    json.dumps(self.settings, indent=4)
                ))
            self.logger.info('Saved settings.cache')
        except Exception as e:
            self.logger.error('Cannot save settings.cache: {}'.format(e))


    def load_settings(self, settings):
        self.settings.update(settings)
        try:
            settings_cache = json.loads(open('settings.cache').read())
            self.settings.update(settings_cache)
            self.logger.info('Loaded settings.cache.')
        except Exception as e:
            self.logger.warning('Did not import cached settings: {}'.format(e))
        # Open local settings and override settings.
        try:
            local_settings = json.loads(open('settings.local').read())
            self.settings.update(local_settings)
            self.logger.info('Loaded settings.local.')
        except Exception as e:
            self.logger.warning('Cannot load settings.local: {}'.format(e))


    def apply_settings(self, new_settings):
        self.logger.info('Apply settings not implemented')


    def get_logger(self):
        logger = logging.getLogger(__name__)
        logger.propagate = 0
        logger.setLevel(level=logging.DEBUG if self.settings.get(
            'Debug') else logging.INFO)
        ch = logging.StreamHandler()
        ch.setLevel(level=logging.DEBUG if self.settings.get(
            'Debug') else logging.INFO)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - %(message)s')
        ch.setFormatter(formatter)
        logger.addHandler(ch)
        return logger


    def stop(self):
        self.logger.debug('Stopping.')
        self.sub_socket.close()
        self.pub_socket.close()
        sys.exit(0)


    def spawn(self, func, *args, **kwargs):
        try:
            self.greenlets.append(gevent.spawn(func, *args, **kwargs))
        except Exception as e:
            self.logger.exception(repr(e))

    def spawn_later(self, delay, func, *args, **kwargs):
        try:
            self.greenlets.append(gevent.spawn_later(delay, func, *args, **kwargs))
        except Exception as e:
            self.logger.exception(repr(e))



    def run(self):
        self.logger.info('Started actor with uid {}.'.format(self.uid))
        gevent.joinall(self.greenlets)


    # Periodic function to check that connection is alive.
    def check_idle(self):
        idle_timeout = self.settings.get('IdleTimeout')
        if not idle_timeout:
            self.logger.info('Idle timeout watchdog disabled.')
            return
        self.logger.info('Idle timeout watchdog started.')
        while True:
            # Check when we last a message.
            now = time.time()
            if now - self.last_msg_time > idle_timeout:
                self.last_msg_time_sum += idle_timeout
                self.logger.warning(
                    'Idle timeout! No messages for last {} seconds.'.format(
                                                        self.last_msg_time_sum))
            gevent.sleep(idle_timeout)


    def subscribe(self, s):
        # Add additional subscriptions here.
        self.logger.debug('Subscribed for {}.'.format(s))
        self.sub_socket.setsockopt(zmq.SUBSCRIBE, b'|{}|'.format(s))


    def receive(self):
        # Actor sibscription receive loop
        self.logger.debug('Receiver has been started.')
        while True:
            header, msg = self.sub_socket.recv_multipart()
            # Update counters
            self.receive_message_count +1
            self.last_msg_time = time.time()
            self.last_msg_time_sum = 0
            msg = json.loads(msg)
            msg.update({'Received': time.time()})
            if self.settings.get('Trace'):
                self.logger.debug('Received: {}'.format(json.dumps(msg, indent=4)))

            # Yes, a bit of magic here for easier use IMHO.
            if hasattr(self, 'on_{}'.format(msg.get('Message'))):
                gevent.spawn(
                    getattr(
                        self, 'on_{}'.format(msg.get('Message'))), msg)
            else:
                self.logger.error('Don\'t know how to handle message: {}'.format(
                    json.dumps(msg, indent=4)))
                continue


    def handle_failure(self, msg):
        # TODO: When asked and got an error, send reply with error info.
        pass


    def _remove_msg_headers(self, msg):
        res = msg.copy()
        for key in msg:
            if key in ['Id', 'To', 'Received', 'From', 'Message', 'SendTime', 'Sequence']:
                res.pop(key)
        return res



    def tell(self, msg):
        # This is used to send a message to the bus.
        self.sent_message_count += 1
        msg.update({
            'Id': uuid.uuid4().hex,
            'SendTime': time.time(),
            'From': self.uid,
            'Sequence': self.sent_message_count,
        })
        if self.settings.get('Trace'):
            self.logger.debug('Telling: {}'.format(json.dumps(
                msg, indent=4
            )))
        self.pub_socket.send_json(msg)


    def ask(self, msg, attempt=1):
        # This is used to send a message to the bus and wait for reply
        self.sent_message_count += 1
        msg_id = uuid.uuid4().hex
        msg.update({
            'Id': msg_id,
            'SendTime': time.time(),
            'From': self.uid,
        })
        if self.settings.get('Trace'):
            self.logger.debug('Asking: {}'.format(json.dumps(
                msg, indent=4
            )))
        try:
            self.req_socket.send_json(msg)
            poll = zmq.Poller()
            poll.register(self.req_socket, zmq.POLLIN)
            socks = dict(poll.poll(5000))
            if socks.get(self.req_socket) == zmq.POLLIN:
                res = self.req_socket.recv_json()
                return res
            else:
                self.logger.warning('No reply received for {}.'.format(json.dumps(msg,
                                                                    indent=4)))
                self.req_socket.setsockopt(zmq.LINGER, 0)
                self.req_socket.close()
                poll.unregister(self.req_socket)
                self.req_socket = self.context.socket(zmq.REQ)
                self.req_socket.connect(self.settings.get('ReqAddr'))
                self.logger.info('Reconnected REQ socket.')
                # Re-Ask second time
                if attempt < 2:
                    self.logger.debug('Asking again.')
                    self.ask(msg, attempt=2)
                else:
                    self.logger.debug('Forgetting.')
        except Exception as e:
            self.logger.error('[Ask] {}'.format(e))


    def ping(self):
        gevent.sleep(10) # Delay before 1-st ping, also allows
        # PingInterval to be received from settings.
        ping_interval = self.settings.get('PingInterval')
        if not ping_interval:
            self.logger.info('Ping disabled.')
            return
        else:
            self.logger.info('Starting ping every {} seconds.'.format(ping_interval))
        gevent.sleep(2) # Give time for subscriber to setup
        while True:
            reply = self.ask({
                'Message': 'Ping',
                'To': self.uid,
            })
            if not reply:
                self.logger.warning('Ping reply is not received.')
            gevent.sleep(ping_interval)


    def watch(self, target):
        # TODO: heartbeat targer
        pass


    @check_reply
    def on_Ping(self, msg):
        self.logger.debug('Ping received from {}.'.format(msg.get('From')))
        if not msg.get('ReplyRequest'):
            new_msg = {
                'Message': 'Pong',
                'To': msg.get('From'),
            }
            self.tell(new_msg)


    @check_reply
    def on_UpdateSettings(self, msg):
        s = self._remove_msg_headers(msg)
        if self.settings.get('Trace'):
            self.logger.debug('[UpdateSettings] Received: {}'.format(
                json.dumps(s, indent=4)))
        else:
            self.logger.info('Settings updated.')
        self.settings.update(s)
        self.apply_settings(s)
        self.save_settings()


    def on_KeepAlive(self, msg):
        self.logger.debug('KeepAlive received.')


    def on_Pong(self, msg):
        self.logger.debug('Pong received.')


    # TODO: Ideas
    def on_Subscribe(self, msg):
        # TODO: Remote subscrube / unsubscribe
        pass


    def on_Start(self, msg):
        # Start /stop remotely local method
        pass
