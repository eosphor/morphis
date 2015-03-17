import llog

import asyncio
from concurrent import futures
import logging
from math import sqrt
import os
import random
import sshtype
import struct
from functools import partial
from datetime import datetime, timedelta

from sqlalchemy import Integer, String, text, func, desc, or_

import bittrie
import packet as mnetpacket
import rsakey
import mn1
import peer as mnpeer
import shell
import enc
from db import Peer
from mutil import hex_dump, log_base2_8bit, hex_string

# Chord Message Types.
CHORD_MSG_GET_PEERS = 110
CHORD_MSG_PEER_LIST = 111
CHORD_MSG_FIND_NODE = 150

BUCKET_SIZE = 2

ZERO_MEM_512_BIT = bytearray(512>>3)

log = logging.getLogger(__name__)

class ChordEngine():
    def __init__(self, node, bind_address=None):
        self.node = node
        self.node_id = enc.generate_ID(node.node_key.asbytes())

        self.loop = node.loop

        self.running = False
        self.server = None #Task.
        self.server_protocol = None

        self.forced_connects = {} # {id, Peer}
        self.pending_connections = {} # {Task, Peer->dbid}
        self.peers = {} # {address: Peer}.
        self.peer_buckets = [{} for i in range(512)] # [{addr: Peer}]
        self.peer_trie = bittrie.BitTrie() # {node_id, Peer}

        self.minimum_connections = 10
        self.maximum_connections = 64
        self.hard_maximum_connections = self.maximum_connections * 2

        self._bind_address = None
        self._bind_port = None

        self._next_request_id = 0

        self._process_connection_count_handle = None
        self._last_process_connection_count = datetime(1, 1, 1)

    @property
    def bind_address(self):
        return self._bind_address

    @bind_address.setter
    def bind_address(self, value):
        self._bind_address = value
        self._bind_port = int(value.split(':')[1])

    @asyncio.coroutine
    def add_peer(self, addr, force=False):
        log.info("Adding peer (addr=[{}]).".format(addr))

        peer = Peer()
        peer.address = addr

        yield from self.add_peers([peer], force)

    @asyncio.coroutine
    def add_peers(self, peers, force=False):
        log.info("Adding {} peers.".format(len(peers)))

        def dbcall():
            with self.node.db.open_session() as sess:
                tlocked = False

                batched = 0
                added = False

                for peer in peers:
                    if not tlocked:
                        self.node.db.lock_table(sess, Peer)
                        tlocked = True

                    q = sess.query(func.count("*"))

                    if peer.pubkey:
                        assert peer.node_id is None
                        peer.node_id = enc.generate_ID(peer.pubkey)
                        mnpeer.update_distance(self.node_id, peer)
                        q = q.filter(Peer.node_id == peer.node_id)
                    elif peer.address:
                        assert peer.node_id is None
                        q = q.filter(Peer.address == peer.address)

                    if q.scalar() > 0:
                        if log.isEnabledFor(logging.DEBUG):
                            log.debug("Peer [{}] already in list.".format(peer.address))
                        continue

                    peer.connected = False

                    sess.add(peer)
                    added = True
                    if force:
                        self.forced_connects[peer.id] = peer

                    batched += 1
                    if batched == 10:
                        sess.commit()
                        tlocked = False
                        batched = 0

                if added and tlocked:
                    sess.commit()

                return added

        added = yield from self.loop.run_in_executor(None, dbcall)

        if added and self.running:
            yield from self._process_connection_count()

    @asyncio.coroutine
    def start(self):
        self.running = True

        host, port = self._bind_address.split(':')
        self.server = self.loop.create_server(\
            self._create_server_protocol, host, port)

#        self.loop.run_until_complete(self.server)
#        asyncio.async(self.server, loop=self.loop)
        yield from self.server

        log.info("Node listening on [{}:{}].".format(host, port))

        yield from self._process_connection_count()

    def stop(self):
        if self.server:
            self.server.close()

    def _async_process_connection_count(self):
        asyncio.async(self._process_connection_count())

    @asyncio.coroutine
    def _process_connection_count(self):
        cnt = len(self.pending_connections) + len(self.peers)
        if cnt >= self.maximum_connections:
            return

        now = datetime.today()
        if cnt >= self.minimum_connections:
            diff = now - self._last_process_connection_count
            if diff < timedelta(seconds=15):
                return

        self._last_process_connection_count = now

        if self._process_connection_count_handle:
            self._process_connection_count_handle.cancel()

        self._process_connection_count_handle =\
            self.loop.call_later(\
                60, self._async_process_connection_count)

        needed = self.maximum_connections - cnt

        yield from self.__process_connection_count(needed)

    @asyncio.coroutine
    def __process_connection_count(self, needed):
        log.info("Processing connection count.")

        # Process any manually requested connects.
        for dbp in self.forced_connects.values():
            peer = yield from self._connect_peer(dbp)
            if peer:
                needed -= 1

        if needed <= 0:
            return

        # If we still need PeerS, then we now use our Chord algorithm.

        def dbcall():
            with self.node.db.open_session() as sess:
                return sess.query(func.min_(Peer.distance))\
                    .filter(Peer.distance != None)\
                    .filter(Peer.distance != 0)\
                    .filter(Peer.connected == False)\
                    .scalar()

        closestdistance =\
            yield from self.loop.run_in_executor(None, dbcall)

        if not closestdistance:
            return

        connect_queues = asyncio.Queue()
        queue_empty = asyncio.Event()
        done = asyncio.Event()

        asyncio.async(\
            self._process_connect_queues(\
                needed, connect_queues, queue_empty, done),\
            loop=self.loop)

        fetched = False
        distance = closestdistance
        while True:
            if distance >= 512 + 1:
                yield from connect_queue.put(None)
                if fetched:
                    fetched = False
                    distance = closestdistance
                    continue
                yield from connect_queues.put(None)
                break

            connect_queue = asyncio.Queue()
            yield from connect_queues.put(connect_queue)

            peer_bucket = self.peer_buckets[distance - 1]
            bucket_needs = BUCKET_SIZE - len(peer_bucket)
            if bucket_needs <= 0:
                distance += 1
                yield from connect_queue.put(None)
                continue

            def dbcall():
                with self.node.db.open_session() as sess:
                    grace = datetime.today() - timedelta(minutes=5)

                    q = sess.query(Peer)\
                        .filter(Peer.distance == distance,\
                            Peer.connected == False,\
                            or_(Peer.last_connect_attempt == None,\
                                Peer.last_connect_attempt < grace))\
                        .order_by(desc(Peer.direction), Peer.node_id)\
                        .limit(bucket_needs * 2)

                    r = q.all()

                    sess.expunge_all()

                    return r

            rs = yield from self.loop.run_in_executor(None, dbcall)

            if not len(rs):
                distance += 1
                yield from connect_queue.put(None)
                continue

            fetched = True

            queue_empty.clear()

            for dbpeer in rs:
                yield from connect_queue.put(dbpeer)

            if done.is_set():
                return
            yield from queue_empty.wait()
            if done.is_set():
                return

            distance += 1
            yield from connect_queue.put(None)

        log.info("No more available PeerS to fetch.")

    @asyncio.coroutine
    def _process_connect_queues(\
            self, needed, connect_queues, queue_empty, done):
        connect_futures = []

        while True:
            # Each queue is for one distance.
            queue = yield from connect_queues.get()
            if not queue:
                break;

            while True:
                if queue.empty():
                    queue_empty.set()
                dbpeer = yield from queue.get()
                if not dbpeer:
                    break

                assert dbpeer.node_id != None

                peer_bucket = self.peer_buckets[dbpeer.distance - 1]
                bucket_needs = BUCKET_SIZE - len(peer_bucket)
                if bucket_needs <= 0:
                    break

                connect_c =\
                    asyncio.async(self._connect_peer(dbpeer), loop=self.loop)

                connect_futures.append(connect_c)

                if len(connect_futures) < 5:
                    continue

                connected, pending = yield from asyncio.wait(\
                    connect_futures, loop=self.loop,\
                    return_when=futures.FIRST_COMPLETED)

                connect_futures = list(pending)

                needed -= len(connected)

                if needed <= 0:
                    log.info("Connected to requested amount of PeerS.")

                    done.set()
                    queue_empty.set() # Wake the db fetch coroutine.

                    if connect_futures:
                        yield from asyncio.wait(connect_futures, loop=self.loop)
                    log.info("Finished connecting.")
                    return

        if connect_futures:
            yield from asyncio.wait(connect_futures, loop=self.loop)
        log.info("Finished connecting all PeerS we could find.")

    @asyncio.coroutine
    def _connect_peer(self, dbpeer):
        log.info("Connecting to peer (id={}, addr=[{}]).".format(dbpeer.id,\
            dbpeer.address))

        host, port = dbpeer.address.split(':')

        peer = mnpeer.Peer(self, dbpeer)

        client = self.loop.create_connection(\
            partial(self._create_client_protocol, peer),\
            host, port)

        def dbcall(dbpeer):
            with self.node.db.open_session() as sess:
                dbpeer = sess.query(Peer).get(dbpeer.id)
                if dbpeer.connected:
                    if log.isEnabledFor(logging.DEBUG):
                        log.debug(\
                            "Peer [{}] connected to us in the mean time."\
                            .format(dbpeer.id))
                    return False

                dbpeer.connected = True
                dbpeer.last_connect_attempt = datetime.today()
                sess.commit()
                return True

        r = yield from self.loop.run_in_executor(None, dbcall, dbpeer)

        if not r:
            return

        try:
            yield from client
        except Exception as ex:
            log.info("Connection to Peer (dbid=[{}]) failed: {}: {}"\
                .format(dbpeer.id, type(ex), ex))

            # An exception on connect; update db, Etc.
            def dbcall(dbpeer):
                with self.node.db.open_session() as sess:
                    dbpeer = sess.query(Peer).get(dbpeer.id)
                    dbpeer.connected = False

                    sess.commit()

            yield from self.loop.run_in_executor(None, dbcall, dbpeer)

            return None

        if dbpeer.node_id:
            if not self.add_to_peers(peer):
                peer.protocol.transport.close()
                return None

        return peer

    def _create_server_protocol(self):
        ph = mn1.SshServerProtocol(self.loop)
        ph.server_key = self.node.node_key

        p = mnpeer.Peer(self)
        p.protocol = ph

#        self.pending_connections.append(p)

        return ph

    def _create_client_protocol(self, peer):
        ph = mn1.SshClientProtocol(self.loop)
        ph.client_key = self.node.node_key

        if peer.node_key:
            ph.server_key = peer.node_key

        peer.protocol = ph

#        self.pending_connections.append(peer)

        return ph

    def connection_made(self, peer):
        pass

    def connection_lost(self, peer, exc):
        asyncio.async(self._connection_lost(peer, exc), loop=self.loop)

    @asyncio.coroutine
    def _connection_lost(self, peer, exc):
        if peer.node_id:
            self.remove_from_peers(peer)

        if not peer.dbid:
            return

        def dbcall():
            with self.node.db.open_session() as sess:
                dbpeer = sess.query(Peer).get(peer.dbid)
                if not dbpeer:
                    # Might have been deleted.
                    return;
                dbpeer.connected = False
                sess.commit()

        yield from self.loop.run_in_executor(None, dbcall)

    @asyncio.coroutine
    def peer_authenticated(self, peer):
        log.info("Peer (dbid={}) has authenticated.".format(peer.dbid))

        add_to_peers = True

        if peer.dbid:
            # This would be an outgoing connection; and thus this dbid does
            # for sure exist in the database.
            def dbcall():
                with self.node.db.open_session() as sess:
                    dbpeer = sess.query(Peer).get(peer.dbid)

                    if not dbpeer.node_id:
                        # Then it was a manually initiated connection (and no
                        # public key was specified).
                        dbpeer.node_id = peer.node_id
                        dbpeer.pubkey = peer.node_key.asbytes()

                        dbpeer.distance = peer.distance
                        dbpeer.direction = peer.direction

                        if dbpeer.distance == 0:
                            log.info("Peer is us! (Has the same ID!)")
                            sess.delete(dbpeer)
                            sess.commit()
                            return False, False

                        sess.commit()
                        sess.expunge(dbpeer)
                        return True, True
                    else:
                        # Then we were trying to connect to a specific node_id.
                        if dbpeer.node_id != peer.node_id:
                            # Then the node we reached is not the node we were
                            # trying to connect to.
                            dbpeer.connected = False
                            sess.commit()
                            return False, False

                        sess.expunge(dbpeer)
                        return True, False # We already did when connecting.

            r, r2 = yield from self.loop.run_in_executor(None, dbcall)
            if not r:
                return False

            if not r2:
                add_to_peers = False
        else:
            # This would be an incoming connection.
            def dbcall():
                with self.node.db.open_session() as sess:
                    self.node.db.lock_table(sess, Peer)
                    dbpeer = sess.query(Peer)\
                        .filter(Peer.node_id == peer.node_id).first()

                    if not dbpeer:
                        # An incoming connection from an unknown Peer.
                        dbpeer = Peer()
                        dbpeer.node_id = peer.node_id
                        dbpeer.pubkey = peer.node_key.asbytes()

                        dbpeer.distance = peer.distance
                        dbpeer.direction = peer.direction

                        if dbpeer.distance == 0:
                            log.info("Peer is us! (Has the same ID!)")
                            return False, None

                        dbpeer.address = peer.address

                        sess.add(dbpeer)
                    else:
                        # Known Peer has connected to us.
                        if dbpeer.distance == 0:
                            log.warning("Found ourselves in the Peer table!")
                            log.info("Peer is us! (Has the same ID!)")
                            dbpeer.connected = False
                            sess.commit()
                            return False, None

                        if dbpeer.connected:
                            log.info("Already connected to Peer, disconnecting redundant connection.")
                            return False, None

                        host, port = dbpeer.address.split(':')
                        if host != peer.protocol.address[0]:
                            log.info("Remote Peer host has changed, updating our db record.")
                            dbpeer.address = "{}:{}".format(\
                            peer.protocol.address[0],\
                            port)

                    dbpeer.connected = True

                    sess.commit()

                    fetch_id_in_thread = dbpeer.id

                    sess.expunge(dbpeer)

                    return True, dbpeer

            r, dbpeer = yield from self.loop.run_in_executor(None, dbcall)
            if not r:
                return False

            peer.dbid = dbpeer.id

            if peer.address != dbpeer.address:
                peer.address = dbpeer.address

        r = self.forced_connects.pop(peer.dbid, None)
        if not r:
            r = yield from self.is_peer_connection_desirable(peer)
            if not r:
                log.info("Peer connection unwanted, disconnecting.")
                return False

        if add_to_peers:
            return self.add_to_peers(peer)

        return True

    def add_to_peers(self, peer):
        existing = self.peers.setdefault(peer.address, peer)
        if existing is not peer:
            log.error("Somehow we are trying to connect to an address [{}] already connected!".format(peer.address))
            return False
        self.peer_buckets[peer.distance - 1][peer.address] = peer

        xorkey = bittrie.XorKey(self.node_id, peer.node_id)
        self.peer_trie[xorkey] = peer

        return True

    def remove_from_peers(self, peer):
        self.peers.pop(peer.address, None)
        self.peer_buckets[peer.distance - 1].pop(peer.address, None)

        xorkey = bittrie.XorKey(self.node_id, peer.node_id)
        del self.peer_trie[xorkey]

    @asyncio.coroutine
    def is_peer_connection_desirable(self, peer):
        peercnt = len(self.peers)

        if peer.protocol.address[0].startswith("127."):
            if peer.protocol.remote_banner.startswith("SSH-2.0-OpenSSH"):
                return True

        if peercnt >= self.hard_maximum_connections:
            return False

        if peer.protocol.server_mode:
            return True

        bucket = self.peer_buckets[peer.distance - 1]

        if len(bucket) < BUCKET_SIZE:
            return True

        # Otherwise check that the node_id is closer than all others.
        xorkey = bittrie.XorKey(self.node_id, peer.node_id)

        cnt = 0
        none_started = False
        for closer_node in self.peer_trie.find(xorkey, False):
            if not closer_node:
                none_started = True
                continue
            elif not none_started:
                # Outgoing connections are expected to be in the peer_trie.
                log.info("Peer already connected, undesirable.")
                return False

            if closer_node.distance != peer.distance:
                # No more closer ones.
                return True

            cnt += 1
            if cnt == BUCKET_SIZE:
                log.info(\
                    "Peer is further than BUCKET_SIZE connected PeerS,"\
                    " undesirable.")
                return False

    @asyncio.coroutine
    def connection_ready(self, peer):
        server_mode = peer.protocol.server_mode

        log.info("Connection to Peer (dbid={}, server_mode={}) is now ready."\
            .format(peer.dbid, server_mode))

        if server_mode:
            # TODO: Do checks, limits, and stuff.
            return;

        # Client requests a GetPeers upon connection.
        asyncio.async(self._send_get_peers(peer), loop=self.loop)

    @asyncio.coroutine
    def _send_get_peers(self, peer):
        local_cid, queue = yield from peer.open_channel("mpeer", True)
        if not queue:
            return

        msg = ChordGetPeers()
        msg.sender_port = self._bind_port

        peer.protocol.write_channel_data(local_cid, msg.encode())

        #TODO: With lightweight channels, we should just close this channel
        # instead.
        asyncio.async(\
            self._process_chord_packet(peer, local_cid, queue),\
            loop=self.loop)
        return

    @asyncio.coroutine
    def request_open_channel(self, peer, req):
        if req.channel_type == "mpeer":
            return True
        elif req.channel_type == "session":
            return peer.protocol.address[0] == "127.0.0.1"
        return False

    @asyncio.coroutine
    def channel_opened(self, peer, channel_type, local_cid):
        if not channel_type:
            # channel_type is None when the we initiated the channel.
            return

        queue = peer.channel_queues[local_cid]

        if channel_type == "mpeer":
            asyncio.async(\
                self._process_chord_packet(peer, local_cid, queue),\
                loop=self.loop)
            return
        elif channel_type == "session":
            nshell = shell.Shell(self.loop, peer, local_cid, queue)
            asyncio.async(nshell.cmdloop(), loop=self.loop)
            return

    @asyncio.coroutine
    def channel_closed(self, peer, local_cid):
        pass

    @asyncio.coroutine
    def channel_data(self, peer, local_cid, data):
        # Return False means to let data go into channel queue.
        return False

    @asyncio.coroutine
    def _process_chord_packet(self, peer, local_cid, queue):
        while True:
            log.info("Waiting for chord packet.")
            packet = yield from queue.get()
            if not packet:
                break
            log.info("Processing chord packet.")
            yield from self.__process_chord_packet(peer, local_cid, packet)

    @asyncio.coroutine
    def __process_chord_packet(self, peer, local_cid, data):
        if log.isEnabledFor(logging.DEBUG):
            log.debug("data=\n[{}].".format(hex_dump(data)))
        msg = ChordMessage(None, data)

        log.info("packet_type=[{}].".format(msg.packet_type))

        if msg.packet_type == CHORD_MSG_GET_PEERS:
            log.info("Received CHORD_MSG_GET_PEERS message.")
            msg = ChordGetPeers(data)

            if peer.protocol.server_mode:
                omsg = ChordGetPeers()
                omsg.sender_port = self._bind_port

                peer.protocol.write_channel_data(local_cid, omsg.encode())

            host, port = peer.address.split(':')

            if int(port) != msg.sender_port:
                log.info(\
                    "Remote Peer said port [{}] has changed, updating our records [{}].".format(msg.sender_port, port))

                self.peers.pop(peer.address, None)
                self.peer_buckets[peer.distance - 1].pop(peer.address, None)

                peer.address = "{}:{}".format(host, msg.sender_port)

                self.peers[peer.address] = peer
                self.peer_buckets[peer.distance - 1][peer.address] = peer

                def dbcall():
                    with self.node.db.open_session() as sess:
                        dbp = sess.query(Peer).get(peer.dbid);
                        dbp.address = peer.address
                        sess.commit()

                yield from self.loop.run_in_executor(None, dbcall)

            pl = list(self.peers.values())
            while True:
                cnt = len(pl)

                msg = ChordPeerList()
                msg.peers = pl[:min(25, cnt)]

                peer.protocol.write_channel_data(local_cid, msg.encode())

                if cnt <= 25:
                    break;

                pl = pl[25:]

        elif msg.packet_type == CHORD_MSG_PEER_LIST:
            log.info("Received CHORD_MSG_PEER_LIST message.")
            msg = ChordPeerList(data)
            yield from self.add_peers(msg.peers)
        elif msg.packet_type == CHORD_MSG_FIND_NODE:
            log.info("Received CHORD_MSG_FIND_NODE message.")
            msg = ChordFindNode(data)

            pt = bittrie.BitTrie()

            for cp in self.peers.values():
                ki = bittrie.XorKey(cp.node_id, msg.node_id)
                pt[ki] = cp

            pt[bittrie.XorKey(self.node_id, msg.node_id)] = True

            cnt = int(sqrt(len(self.peers)))
            log.info("Relaying to upto {} nodes.".format(cnt))
            rlist = []

            for r in pt.find(ZERO_MEM_512_BIT):
                if not r:
                    continue;

                if r is True:
                    log.info("No more nodes closer than ourselves.")
                    break

                log.debug("nn: {} FOUND: {:04} {:22} diff=[{}]".format(self.node.instance, r.dbid, r.address, hex_string([x ^ y for x, y in zip(r.node_id, msg.node_id)])))

                rlist.append(r)

                cnt -= 1
                if not cnt:
                    break

            if not rlist:
                rmsg = ChordPeerList()
                rmsg.peers = []

                peer.protocol.write_channel_data(local_cid, rmsg.encode())
                peer.protocol.close_channel(local_cid)

                return

            rmsg = ChordPeerList()
            rmsg.peers = rlist

            peer.protocol.write_channel_data(local_cid, rmsg.encode())

            tasks = []

            for r in rlist:
                @asyncio.coroutine
                def _run(r):
                    cid, queue = yield from r.open_channel("mpeer", True)
                    if not queue:
                        return

                    r.protocol.write_channel_data(cid, data)

                    while True:
                        pkt = yield from queue.get()
                        if not pkt:
                            return

                        # Test that it is valid.
                        try:
                            chord.ChordPeerList(pkt)
                        except:
                            break

                        peer.protocol.write_channel_data(local_cid, pkt)

                tasks.append(asyncio.async(_run(r), loop=self.loop))

            yield from asyncio.wait(tasks, loop=self.loop)

            peer.protocol.close_channel(local_cid)

# Example of non-working db code. Sqlite seems to break when order by contains
# any bitwise operations. (It just returns the rows in order of id.)
#            def dbcall():
#                with self.node.db.open_session() as sess:
#                    t = text(\
#                        "SELECT id, address FROM peer ORDER BY"\
#                        " (~(node_id&:r_id))&(node_id|:r2_id) DESC"\
#                        " LIMIT :lim")
#
#                    st = t.bindparams(r_id = msg.node_id, r2_id = msg.node_id,\
#                            lim = BUCKET_SIZE)\
#                        .columns(id=Integer, address=String)
#
#                    rs = sess.connection().execute(st)
#
#                    for r in rs:
#                        log.info("nn: {} FOUND: {} {}".format(self.node.instance, r.id, r.address))
#
#            yield from self.loop.run_in_executor(None, dbcall)
        else:
            log.warning("Ignoring unrecognized packet.")

class ChordMessage(object):
    def __init__(self, packet_type = None, buf = None):
        self.buf = buf
        self.packet_type = packet_type

        if not buf:
            return

        self.parse()

        if packet_type and self.packet_type != packet_type:
            raise Exception("Expecting packet type [{}] but got [{}].".format(packet_type, self.packet_type))

    def parse(self):
        self.packet_type = struct.unpack("B", self.buf[0:1])[0]

    def encode(self):
        nbuf = bytearray()
        nbuf += struct.pack("B", self.packet_type & 0xff)

        self.buf = nbuf

        return nbuf

class ChordGetPeers(ChordMessage):
    def __init__(self, buf = None):
        self.sender_port = None

        super().__init__(CHORD_MSG_GET_PEERS, buf)

    def encode(self):
        nbuf = super().encode()

        nbuf += struct.pack(">L", self.sender_port)

        return nbuf

    def parse(self):
        super().parse()

        i = 1
        self.sender_port = struct.unpack(">L", self.buf[i:i+4])[0]

class ChordPeerList(ChordMessage):
    def __init__(self, buf = None):
        self.peers = [] # [Peer]

        super().__init__(CHORD_MSG_PEER_LIST, buf)

    def encode(self):
        nbuf = super().encode()
        nbuf += struct.pack(">L", len(self.peers))
        for peer in self.peers:
            nbuf += sshtype.encodeString(peer.address)
#            nbuf += sshtype.encodeBinary(peer.node_id)
            if type(peer) is mnpeer.Peer:
                nbuf += sshtype.encodeBinary(peer.node_key.asbytes())
            else:
                assert type(peer) is Peer
                nbuf += sshtype.encodeBinary(peer.pubkey)

        return nbuf

    def parse(self):
        super().parse()
        i = 1
        pcnt = struct.unpack(">L", self.buf[i:i+4])[0]
        i += 4
        self.peers = []
        for n in range(pcnt):
            log.debug("Reading record {}.".format(n))
            peer = Peer()
            l, peer.address = sshtype.parseString(self.buf[i:])
            i += l
#            l, peer.node_id = sshtype.parseBinary(self.buf[i:])
#            i += l
            l, peer.pubkey = sshtype.parseBinary(self.buf[i:])
            i += l

            if not check_address(peer.address):
                continue

            self.peers.append(peer)

class ChordFindNode(ChordMessage):
    def __init__(self, buf = None):
        self.node_id = None

        super().__init__(CHORD_MSG_FIND_NODE, buf)

    def encode(self):
        nbuf = super().encode()
        nbuf += self.node_id

        return nbuf

    def parse(self):
        super().parse()
        i = 1
        self.node_id = self.buf[i:]

def check_address(address):
    return True
