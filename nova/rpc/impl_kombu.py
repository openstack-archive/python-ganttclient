# vim: tabstop=4 shiftwidth=4 softtabstop=4

#    Copyright 2011 OpenStack LLC
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from nova import flags
from nova import log as logging

import kombu
import kombu.entity
import kombu.messaging
import kombu.connection
import itertools
import sys
import time
import uuid


FLAGS = flags.FLAGS
LOG = logging.getLogger('nova.rpc')

flags.DEFINE_integer('rpc_conn_pool_size', 30,
                     'Size of RPC connection pool')
flags.DEFINE_integer('rpc_thread_pool_size', 1024,
                     'Size of RPC thread pool')


class QueueBase(object):
    """Queue base class."""

    def __init__(self, channel, callback, tag, **kwargs):
        """Init the queue.

        'channel' is the amqp channel to use
        'callback' is the callback to call when messages are received
        'tag' is a unique ID for the consumer on the channel

        queue name, exchange name, and other kombu options are
        passed in here as a dictionary.
        """
        self.callback = callback
        self.tag = str(tag)
        self.kwargs = kwargs
        self.queue = None
        self.reconnect(channel)

    def reconnect(self, channel):
        """Re-create the queue after a rabbit reconnect"""
        self.channel = channel
        self.kwargs['channel'] = channel
        self.queue = kombu.entity.Queue(**self.kwargs)
        self.queue.declare()

    def consume(self, *args, **kwargs):
        """Consume from this queue.
        If a callback is specified in kwargs, use that.  Otherwise,
        use the callback passed during __init__()

        The callback will be called if a message was read off of the
        queue.

        If kwargs['nowait'] is True, then this call will block until
        a message is read.

        Messages will automatically be acked if the callback doesn't
        raise an exception
        """

        options = {'consumer_tag': self.tag}
        options['nowait'] = kwargs.get('nowait', False)
        callback = kwargs.get('callback', self.callback)
        if not callback:
            raise ValueError("No callback defined")

        def _callback(raw_message):
            message = self.channel.message_to_python(raw_message)
            callback(message.payload)
            message.ack()

        self.queue.consume(*args, callback=_callback, **options)

    def cancel(self):
        """Cancel the consuming from the queue, if it has started"""
        try:
            self.queue.cancel(self.tag)
        except KeyError, e:
            # NOTE(comstud): Kludge to get around a amqplib bug
            if str(e) != "u'%s'" % self.tag:
                raise
        self.queue = None


class DirectQueue(QueueBase):
    """Queue/consumer class for 'direct'"""

    def __init__(self, channel, msg_id, callback, tag, **kwargs):
        """Init a 'direct' queue.

        'channel' is the amqp channel to use
        'msg_id' is the msg_id to listen on
        'callback' is the callback to call when messages are received
        'tag' is a unique ID for the consumer on the channel

        Other kombu options may be passed
        """
        # Default options
        options = {'durable': False,
                'auto_delete': True,
                'exclusive': True}
        options.update(kwargs)
        exchange = kombu.entity.Exchange(
                name=msg_id,
                type='direct',
                durable=options['durable'],
                auto_delete=options['auto_delete'])
        super(DirectQueue, self).__init__(
                channel,
                callback,
                tag,
                name=msg_id,
                exchange=exchange,
                routing_key=msg_id,
                **options)


class TopicQueue(QueueBase):
    """Queue/consumer class for 'topic'"""

    def __init__(self, channel, topic, callback, tag, **kwargs):
        """Init a 'topic' queue.

        'channel' is the amqp channel to use
        'topic' is the topic to listen on
        'callback' is the callback to call when messages are received
        'tag' is a unique ID for the consumer on the channel

        Other kombu options may be passed
        """
        # Default options
        options = {'durable': FLAGS.rabbit_durable_queues,
                'auto_delete': False,
                'exclusive': False}
        options.update(kwargs)
        exchange = kombu.entity.Exchange(
                name=FLAGS.control_exchange,
                type='topic',
                durable=options['durable'],
                auto_delete=options['auto_delete'])
        super(TopicQueue, self).__init__(
                channel,
                callback,
                tag,
                name=topic,
                exchange=exchange,
                routing_key=topic,
                **options)


class FanoutQueue(QueueBase):
    """Queue/consumer class for 'fanout'"""

    def __init__(self, channel, topic, callback, tag, **kwargs):
        """Init a 'fanout' queue.

        'channel' is the amqp channel to use
        'topic' is the topic to listen on
        'callback' is the callback to call when messages are received
        'tag' is a unique ID for the consumer on the channel

        Other kombu options may be passed
        """
        unique = uuid.uuid4().hex
        exchange_name = '%s_fanout' % topic
        queue_name = '%s_fanout_%s' % (topic, unique)

        # Default options
        options = {'durable': False,
                'auto_delete': True,
                'exclusive': True}
        options.update(kwargs)
        exchange = kombu.entity.Exchange(
                name=exchange_name,
                type='fanout',
                durable=options['durable'],
                auto_delete=options['auto_delete'])
        super(FanoutQueue, self).__init__(
                channel,
                callback,
                tag,
                name=queue_name,
                exchange=exchange,
                routing_key=topic,
                **options)


class Publisher(object):
    """Base Publisher class"""

    def __init__(self, channel, exchange_name, routing_key, **kwargs):
        """Init the Publisher class with the exchange_name, routing_key,
        and other options
        """
        self.exchange_name = exchange_name
        self.routing_key = routing_key
        self.kwargs = kwargs
        self.reconnect(channel)

    def reconnect(self, channel):
        """Re-establish the Producer after a rabbit reconnection"""
        self.exchange = kombu.entity.Exchange(name=self.exchange_name,
                **self.kwargs)
        self.producer = kombu.messaging.Producer(exchange=self.exchange,
                channel=channel, routing_key=self.routing_key)

    def send(self, msg):
        """Send a message"""
        self.producer.publish(msg)


class DirectPublisher(Publisher):
    """Publisher class for 'direct'"""
    def __init__(self, channel, msg_id, **kwargs):
        """init a 'direct' publisher.

        Kombu options may be passed as keyword args to override defaults
        """

        options = {'durable': False,
                'auto_delete': True,
                'exclusive': True}
        options.update(kwargs)
        super(DirectPublisher, self).__init__(channel,
                msg_id,
                msg_id,
                type='direct',
                **options)


class TopicPublisher(Publisher):
    """Publisher class for 'topic'"""
    def __init__(self, channel, topic, **kwargs):
        """init a 'topic' publisher.

        Kombu options may be passed as keyword args to override defaults
        """
        options = {'durable': FLAGS.rabbit_durable_queues,
                'auto_delete': False,
                'exclusive': False}
        options.update(kwargs)
        super(TopicPublisher, self).__init__(channel,
                FLAGS.control_exchange,
                topic,
                type='topic',
                **options)


class FanoutPublisher(Publisher):
    """Publisher class for 'fanout'"""
    def __init__(self, channel, topic, **kwargs):
        """init a 'fanout' publisher.

        Kombu options may be passed as keyword args to override defaults
        """
        options = {'durable': False,
                'auto_delete': True,
                'exclusive': True}
        options.update(kwargs)
        super(FanoutPublisher, self).__init__(channel,
                '%s_fanout' % topic,
                None,
                type='fanout',
                **options)


class Connection(object):
    """Connection instance object."""

    def __init__(self):
        self.queues = []
        self.max_retries = FLAGS.rabbit_max_retries
        self.interval_start = FLAGS.rabbit_retry_interval
        self.interval_stepping = 0
        self.interval_max = FLAGS.rabbit_retry_interval

        self.params = dict(hostname=FLAGS.rabbit_host,
                          port=FLAGS.rabbit_port,
                          userid=FLAGS.rabbit_userid,
                          password=FLAGS.rabbit_password,
                          virtual_host=FLAGS.rabbit_virtual_host)
        if FLAGS.fake_rabbit:
            self.params['transport'] = 'memory'
        self.connection = None
        self.reconnect()

    @classmethod
    def instance(cls, new=True):
        """Returns the instance."""
        if new or not hasattr(cls, '_instance'):
            if new:
                return cls()
            else:
                cls._instance = cls()
        return cls._instance

    def reconnect(self):
        """Handles reconnecting and re-estblishing queues"""
        if self.connection:
            try:
                self.connection.close()
            except self.connection.connection_errors:
                pass
            time.sleep(1)
        self.connection = kombu.connection.Connection(**self.params)
        self.queue_num = itertools.count(1)

        try:
            self.connection.ensure_connection(errback=self.connect_error,
                    max_retries=self.max_retries,
                    interval_start=self.interval_start,
                    interval_step=self.interval_stepping,
                    interval_max=self.interval_max)
        except self.connection.connection_errors, e:
            err_str = str(e)
            max_retries = FLAGS.rabbit_max_retries
            LOG.error(_('Unable to connect to AMQP server '
                    'after %(max_retries)d tries: %(err_str)s') % locals())
            # NOTE(comstud): Original carrot code exits after so many
            # attempts, but I wonder if we should re-try indefinitely
            sys.exit(1)
        LOG.info(_('Connected to AMQP server on %(hostname)s:%(port)d' %
                self.params))
        self.channel = self.connection.channel()
        for consumer in self.queues:
            consumer.reconnect(self.channel)
        if self.queues:
            LOG.debug(_("Re-established AMQP queues"))

    def get_channel(self):
        """Convenience call for bin/clear_rabbit_queues"""
        return self.channel

    def connect_error(self, exc, interval):
        """Callback when there are connection re-tries by kombu"""
        info = self.params.copy()
        info['intv'] = interval
        info['e'] = exc
        LOG.error(_('AMQP server on %(hostname)s:%(port)d is'
                ' unreachable: %(e)s. Trying again in %(intv)d'
                ' seconds.') % info)

    def close(self):
        """Close/release this connection"""
        self.connection.release()
        self.connection = None

    def reset(self):
        """Reset a connection so it can be used again"""
        self.channel.close()
        self.channel = self.connection.channel()
        self.queues = []

    def create_queue(self, queue_cls, topic, callback):
        """Create a queue using the class that was passed in and
        add it to our list of queues used for consuming
        """
        queue = queue_cls(self.channel, topic, callback,
                self.queue_num.next())
        self.queues.append(queue)
        return queue

    def consume(self, limit=None):
        """Consume from all queues"""
        while True:
            try:
                queues_head = self.queues[:-1]
                queues_tail = self.queues[-1]
                for queue in queues_head:
                    queue.consume(nowait=True)
                queues_tail.consume(nowait=False)

                for iteration in itertools.count(0):
                    if limit and iteration >= limit:
                        raise StopIteration
                    yield self.connection.drain_events()
            except self.connection.connection_errors, e:
                LOG.exception(_('Failed to consume message from queue: '
                        '%s' % str(e)))
                self.reconnect()

    def publisher_send(self, cls, topic, msg):
        """Send to a publisher based on the publisher class"""
        while True:
            publisher = None
            try:
                publisher = cls(self.channel, topic)
                publisher.send(msg)
                return
            except self.connection.connection_errors, e:
                LOG.exception(_('Failed to publish message %s' % str(e)))
                try:
                    self.reconnect()
                    if publisher:
                        publisher.reconnect(self.channel)
                except self.connection.connection_errors, e:
                    pass

    def direct_consumer(self, topic, callback):
        """Create a 'direct' queue.
        In nova's use, this is generally a msg_id queue used for
        responses for call/multicall
        """
        return self.create_queue(DirectQueue, topic, callback)

    def topic_consumer(self, topic, callback=None):
        """Create a 'topic' queue."""
        return self.create_queue(TopicQueue, topic, callback)

    def fanout_consumer(self, topic, callback):
        """Create a 'fanout' queue"""
        return self.create_queue(FanoutQueue, topic, callback)

    def direct_send(self, msg_id, msg):
        """Send a 'direct' message"""
        self.publisher_send(DirectPublisher, msg_id, msg)

    def topic_send(self, topic, msg):
        """Send a 'topic' message"""
        self.publisher_send(TopicPublisher, topic, msg)

    def fanout_send(self, topic, msg):
        """Send a 'fanout' message"""
        self.publisher_send(FanoutPublisher, topic, msg)


class Pool(pools.Pool):
    """Class that implements a Pool of Connections."""

    # TODO(comstud): Timeout connections not used in a while
    def create(self):
        LOG.debug('Creating new connection')
        return RPCIMPL.Connection()

# Create a ConnectionPool to use for RPC calls.  We'll order the
# pool as a stack (LIFO), so that we can potentially loop through and
# timeout old unused connections at some point
ConnectionPool = Pool(
        max_size=FLAGS.rpc_conn_pool_size,
        order_as_stack=True)


class ConnectionContext(object):
    def __init__(self, pooled=True):
        self.connection = None
        if pooled:
            self.connection = ConnectionPool.get()
        else:
            self.connection = RPCIMPL.Connection()
        self.pooled = pooled

    def __enter__(self):
        return self

    def _done(self):
        if self.connection:
            if self.pooled:
                # Reset the connection so it's ready for the next caller
                # to grab from the pool
                self.connection.reset()
                ConnectionPool.put(self.connection)
            else:
                try:
                    self.connection.close()
                except Exception:
                    # There's apparently a bug in kombu 'memory' transport
                    # which causes an assert failure.
                    # But, we probably want to ignore all exceptions when
                    # trying to close a connection, anyway...
                    pass
            self.connection = None

    def __exit__(self, t, v, tb):
        """end if 'with' statement.  We're done here."""
        self._done()

    def __del__(self):
        """Put Connection back into the pool if this ConnectionContext
        is being deleted
        """
        self._done()

    def close(self):
        self._done()

    def __getattr__(self, key):
        if self.connection:
            return getattr(self.connection, key)
        else:
            raise exception.InvalidRPCConnectionReuse()


class ProxyCallback(object):
    """Calls methods on a proxy object based on method and args."""

    def __init__(self, proxy):
        self.proxy = proxy
        self.pool = greenpool.GreenPool(FLAGS.rpc_thread_pool_size)

    def __call__(self, message_data):
        """Consumer callback to call a method on a proxy object.

        Parses the message for validity and fires off a thread to call the
        proxy object method.

        Message data should be a dictionary with two keys:
            method: string representing the method to call
            args: dictionary of arg: value

        Example: {'method': 'echo', 'args': {'value': 42}}

        """
        LOG.debug(_('received %s') % message_data)
        ctxt = _unpack_context(message_data)
        method = message_data.get('method')
        args = message_data.get('args', {})
        if not method:
            LOG.warn(_('no method for message: %s') % message_data)
            ctxt.reply(_('No method for message: %s') % message_data)
            return
        self.pool.spawn_n(self._process_data, ctxt, method, args)

    @exception.wrap_exception()
    def _process_data(self, ctxt, method, args):
        """Thread that maigcally looks for a method on the proxy
        object and calls it.
        """

        node_func = getattr(self.proxy, str(method))
        node_args = dict((str(k), v) for k, v in args.iteritems())
        # NOTE(vish): magic is fun!
        try:
            rval = node_func(context=ctxt, **node_args)
            # Check if the result was a generator
            if isinstance(rval, types.GeneratorType):
                for x in rval:
                    ctxt.reply(x, None)
            else:
                ctxt.reply(rval, None)
            # This final None tells multicall that it is done.
            ctxt.reply(None, None)
        except Exception as e:
            logging.exception('Exception during message handling')
            ctxt.reply(None, sys.exc_info())
        return


def _unpack_context(msg):
    """Unpack context from msg."""
    context_dict = {}
    for key in list(msg.keys()):
        # NOTE(vish): Some versions of python don't like unicode keys
        #             in kwargs.
        key = str(key)
        if key.startswith('_context_'):
            value = msg.pop(key)
            context_dict[key[9:]] = value
    context_dict['msg_id'] = msg.pop('_msg_id', None)
    LOG.debug(_('unpacked context: %s'), context_dict)
    return RpcContext.from_dict(context_dict)


def _pack_context(msg, context):
    """Pack context into msg.

    Values for message keys need to be less than 255 chars, so we pull
    context out into a bunch of separate keys. If we want to support
    more arguments in rabbit messages, we may want to do the same
    for args at some point.

    """
    context_d = dict([('_context_%s' % key, value)
                      for (key, value) in context.to_dict().iteritems()])
    msg.update(context_d)


class RpcContext(context.RequestContext):
    def __init__(self, *args, **kwargs):
        msg_id = kwargs.pop('msg_id', None)
        self.msg_id = msg_id
        super(RpcContext, self).__init__(*args, **kwargs)

    def reply(self, *args, **kwargs):
        if self.msg_id:
            msg_reply(self.msg_id, *args, **kwargs)


class MulticallWaiter(object):
    def __init__(self, connection):
        self._connection = connection
        self._iterator = connection.consume()
        self._result = None
        self._done = False

    def done(self):
        self._done = True
        self._connection = None

    def __call__(self, data):
        """The consume() callback will call this.  Store the result."""
        if data['failure']:
            self._result = RemoteError(*data['failure'])
        else:
            self._result = data['result']

    def __iter__(self):
        if self._done:
            raise StopIteration
        while True:
            self._iterator.next()
            result = self._result
            if isinstance(result, Exception):
                self.done()
                raise result
            if result == None:
                self.done()
                raise StopIteration
            yield result


def create_consumer(conn, topic, proxy, fanout=False):
    """Create a consumer that calls a method in a proxy object"""
    if fanout:
        return conn.fanout_consumer(topic, ProxyCallback(proxy))
    else:
        return conn.topic_consumer(topic, ProxyCallback(proxy))


def create_consumer_set(conn, consumers):
    # FIXME(comstud): Replace this however necessary
    # Returns an object that you can call .wait() on to consume
    # all queues?
    # Needs to have a .close() which will stop consuming?
    # Needs to also have an attach_to_eventlet method for tests?
    raise NotImplemented


def multicall(context, topic, msg):
    """Make a call that returns multiple times."""
    # Can't use 'with' for multicall, as it returns an iterator
    # that will continue to use the connection.  When it's done,
    # connection.close() will get called which will put it back into
    # the pool
    LOG.debug(_('Making asynchronous call on %s ...'), topic)
    msg_id = uuid.uuid4().hex
    msg.update({'_msg_id': msg_id})
    LOG.debug(_('MSG_ID is %s') % (msg_id))
    _pack_context(msg, context)

    conn = ConnectionContext()
    wait_msg = MulticallWaiter(conn)
    conn.direct_consumer(msg_id, wait_msg)
    conn.topic_send(topic, msg)

    return wait_msg


def call(context, topic, msg):
    """Sends a message on a topic and wait for a response."""
    rv = multicall(context, topic, msg)
    # NOTE(vish): return the last result from the multicall
    rv = list(rv)
    if not rv:
        return
    return rv[-1]


def cast(context, topic, msg):
    """Sends a message on a topic without waiting for a response."""
    LOG.debug(_('Making asynchronous cast on %s...'), topic)
    _pack_context(msg, context)
    with ConnectionContext() as conn:
        conn.topic_send(topic, msg)


def fanout_cast(context, topic, msg):
    """Sends a message on a fanout exchange without waiting for a response."""
    LOG.debug(_('Making asynchronous fanout cast...'))
    _pack_context(msg, context)
    with ConnectionContext() as conn:
        conn.fanout_send(topic, msg)


def msg_reply(msg_id, reply=None, failure=None):
    """Sends a reply or an error on the channel signified by msg_id.

    Failure should be a sys.exc_info() tuple.

    """
    with ConnectionContext() as conn:
        if failure:
            message = str(failure[1])
            tb = traceback.format_exception(*failure)
            LOG.error(_("Returning exception %s to caller"), message)
            LOG.error(tb)
            failure = (failure[0].__name__, str(failure[1]), tb)

        try:
            msg = {'result': reply, 'failure': failure}
        except TypeError:
            msg = {'result': dict((k, repr(v))
                            for k, v in reply.__dict__.iteritems()),
                    'failure': failure}
        conn.direct_send(msg_id, msg)
