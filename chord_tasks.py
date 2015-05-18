import llog

import asyncio
from collections import namedtuple
from datetime import datetime
import logging
import math
import os

from sqlalchemy import func

import bittrie
import chord
import chord_packet as cp
from chordexception import ChordException
from db import Peer, DataBlock, NodeState
from mutil import hex_string, page_query
import enc
import peer as mnpeer
import node as mnnode

log = logging.getLogger(__name__)

class Counter(object):
    def __init__(self, value=None):
        self.value = value

#TODO: The existence of the following (DataResponseWrapper) is the indicator
# that we should really refactor this whole file into a new class that is an
# instance per request.
class DataResponseWrapper(object):
    def __init__(self, data_key):
        self.data_key = data_key
        self.data = None

class TunnelMeta(object):
    def __init__(self, peer=None, jobs=None):
        assert type(peer) is mnpeer.Peer

        self.peer = peer
        self.queue = None
        self.local_cid = None
        self.jobs = jobs
        self.task_running = False

class VPeer(object):
    def __init__(self, peer=None, path=None, tun_meta=None):
        # self.peer can be a mnpeer.Peer for immediate Peer, or a db.Peer for
        # a non immediate (tunneled) Peer.
        assert type(peer) is Peer or type(peer) is mnpeer.Peer

        self.peer = peer
        self.path = path
        self.tun_meta = tun_meta
        self.used = False
        self.will_store = False
        self.data_present = False

EMPTY_PEER_LIST_MESSAGE = cp.ChordPeerList(peers=[])
EMPTY_PEER_LIST_PACKET = EMPTY_PEER_LIST_MESSAGE.encode()

class ChordTasks(object):
    def __init__(self, engine):
        self.engine = engine
        self.loop = engine.loop

    @asyncio.coroutine
    def send_node_info(self, peer):
        log.info("Sending ChordNodeInfo message.")

        local_cid, queue =\
            yield from peer.protocol.open_channel("mpeer", True)
        if not queue:
            return

        msg = cp.ChordNodeInfo()
        msg.sender_address = self.engine.bind_address

        peer.protocol.write_channel_data(local_cid, msg.encode())

        data = yield from queue.get()
        if not data:
            return

        msg = cp.ChordNodeInfo(data)
        log.info("Received ChordNodeInfo message.")

        yield from peer.protocol.close_channel(local_cid)

        yield from self.engine._check_update_remote_address(msg, peer)

    @asyncio.coroutine
    def do_stabilize(self):
        if not self.engine.peers:
            log.info("No connected nodes, unable to perform stabilize.")
            return

        # Fetch closest to ourselves.
        closest_nodes = yield from\
            self._do_stabilize(self.engine.node_id, self.engine.peer_trie)

        closest_found_distance =\
            closest_nodes[0].distance if closest_nodes else None

        # Fetch furthest from ourselves.
        node_id = bytearray(self.engine.node_id)
        for i in range(len(node_id)):
            node_id[i] = (~node_id[i]) & 0xFF

        furthest_nodes = yield from self._do_stabilize(node_id)

        if not closest_found_distance:
            closest_found_distance = chord.NODE_ID_BITS
            if furthest_nodes:
                for node in furthest_nodes:
                    if node.distance < closest_found_distance:
                        closest_found_distance = node.distance

            if closest_found_distance is chord.NODE_ID_BITS:
                log.info("Don't know how close a bucket to stop at so not"\
                    " searching inbetween closest and furthest.")
                return

        # Fetch each bucket starting at furthest, stopping when we get to the
        # closest that we found above.
        node_id = bytearray(self.engine.node_id)
        for bit in range(chord.NODE_ID_BITS-1, -1, -1):
            if log.isEnabledFor(logging.INFO):
                log.info("Performing FindNode for bucket [{}]."\
                    .format(bit+1))

            if bit != chord.NODE_ID_BITS-1:
                byte_ = chord.NODE_ID_BYTES - 1 - ((bit+1) >> 3)
                node_id[byte_] ^= 1 << ((bit+1) % 8) # Undo last change.
            byte_ = chord.NODE_ID_BYTES - 1 - (bit >> 3)
            node_id[byte_] ^= 1 << (bit % 8)

            assert self.engine.calc_log_distance(\
                node_id, self.engine.node_id)[0] == (bit + 1),\
                "calc={}, bit={}, diff={}."\
                    .format(\
                        self.engine.calc_log_distance(\
                            node_id, self.engine.node_id)[0],\
                        bit + 1,
                        hex_string(\
                            self.engine.calc_raw_distance(\
                                self.engine.node_id, node_id)))

            nodes = yield from self._do_stabilize(node_id)

            if not closest_found_distance and not nodes:
                break
            elif bit+1 == closest_found_distance:
                break;

    @asyncio.coroutine
    def _do_stabilize(self, node_id, input_trie=None):
        "Returns found nodes sorted by closets."

        conn_nodes = yield from\
            self.send_find_node(node_id, input_trie)

        if not conn_nodes:
            return

        for node in conn_nodes:
            # Do not trust hearsay node_id; add_peers will recalculate it from
            # the public key.
            node.node_id = None

        yield from self.engine.add_peers(conn_nodes)

        return conn_nodes

    @asyncio.coroutine
    def send_get_data(self, data_key):
        assert type(data_key) is bytes

        data_id = enc.generate_ID(data_key)

        data = yield from self.send_find_node(\
            data_id, for_data=True, data_key=data_key)

        return data

    @asyncio.coroutine
    def send_store_data(self, data, key_callback=None):
        # data_id is a double hash due to the anti-entrapment feature.
        data_key = enc.generate_ID(data)
        if key_callback:
            key_callback(data_key)
        data_id = enc.generate_ID(data_key)

        yield from self.send_find_node(data_id, for_data=True, data=data)

    @asyncio.coroutine
    def send_find_node(self, node_id, input_trie=None, for_data=False,\
            data=None, data_key=None):
        "Returns found nodes sorted by closets. If for_data is True then"\
        " this is really {get/store}_data instead of find_node. If data is"\
        " None than it is get_data and the data is returned. Store data"\
        " currently returns nothing."

        if for_data:
            data_mode = cp.DataMode.get if data is None else cp.DataMode.store
        else:
            data_mode = cp.DataMode.none

        if not self.engine.peers:
            log.info("No connected nodes, unable to send FindNode.")
            return

        if not input_trie:
            input_trie = bittrie.BitTrie()
            for peer in self.engine.peer_trie:
                key = bittrie.XorKey(node_id, peer.node_id)
                input_trie[key] = peer

        max_concurrent_queries = 3

        def dbcall():
            with self.engine.node.db.open_session() as sess:
                st = sess.query(Peer).statement.with_only_columns(\
                    [func.count('*')])
                return sess.execute(st).scalar()

        known_peer_cnt = yield from self.loop.run_in_executor(None, dbcall)
        maximum_depth = int(math.log(known_peer_cnt, 2))

        if log.isEnabledFor(logging.INFO):
            log.info("Performing FindNode (data_mode={}) to a max depth of"\
                " [{}].".format(data_mode, maximum_depth))

        result_trie = bittrie.BitTrie()

        # Store ourselves to ignore when peers respond with us in their list.
        result_trie[bittrie.XorKey(node_id, self.engine.node_id)] = False

        tasks = []
        used_tunnels = {}
        far_peers_by_path = {}

        for peer in input_trie:
            key = bittrie.XorKey(node_id, peer.node_id)
            vpeer = VPeer(peer)
            # Store immediate PeerS in the result_trie.
            result_trie[key] = vpeer

            if len(tasks) == max_concurrent_queries:
                continue
            if not peer.ready():
                continue

            tun_meta = TunnelMeta(peer)
            used_tunnels[vpeer] = tun_meta

            tasks.append(self._send_find_node(\
                vpeer, node_id, result_trie, tun_meta, data_mode,\
                far_peers_by_path))

        if not tasks:
            log.info("Cannot perform FindNode, as we know no closer nodes.")
            return

        if log.isEnabledFor(logging.DEBUG):
            log.debug("Starting {} root level FindNode tasks."\
                .format(len(tasks)))

        done, pending = yield from asyncio.wait(tasks, loop=self.loop)

        query_cntr = Counter(0)
        task_cntr = Counter(0)
        done_all = asyncio.Event(loop=self.loop)

        sent_data_request = Counter(0)
        data_rw = DataResponseWrapper(data_key)

        for depth in range(1, maximum_depth):
            direct_peers_lower = 0
            for row in result_trie:
                if row is False:
                    # Row is ourself. Prevent infinite loops.
                    # Sometimes we are looking closer than ourselves, sometimes
                    # further (stabilize vs other). We could use this to end
                    # the loop maybe, do checks. For now, just ignoring it to
                    # prevent infinite loops.
                    continue
                if row.path is None:
                    # Already asked direct peers, and only asking ones we
                    # haven't asked before.
                    direct_peers_lower += 1
                    if direct_peers_lower == len(used_tunnels):
                        # Only deal with results closer than the furthest of
                        # the direct peers we used.
                        break
                    continue

                if row.used:
                    continue

                tun_meta = row.tun_meta
                if not tun_meta.queue:
                    continue

                if log.isEnabledFor(logging.DEBUG):
                    log.debug("Sending FindNode to path [{}]."\
                        .format(row.path))

                pkt = self._generate_relay_packets(row.path)

                tun_meta.peer.protocol.write_channel_data(\
                    tun_meta.local_cid, pkt)

                row.used = True
                query_cntr.value += 1

                if tun_meta.jobs is None:
                    # If this is the first relay for this tunnel, then start a
                    # _process_find_node_relay task for that tunnel.
                    tun_meta.jobs = 1
                    task_cntr.value += 1
                    asyncio.async(\
                        self._process_find_node_relay(\
                            node_id, tun_meta, query_cntr, done_all,\
                            task_cntr, result_trie, data_mode,\
                            far_peers_by_path, sent_data_request, data_rw),\
                        loop=self.loop)
                else:
                    tun_meta.jobs += 1

                if query_cntr.value == max_concurrent_queries:
                    break

            if not query_cntr.value:
                log.info("FindNode search has ended at closest nodes.")
                break

            yield from done_all.wait()
            done_all.clear()

            assert query_cntr.value == 0

            if not task_cntr.value:
                log.info("All tasks exited.")
                break

        if data_mode.value:
            msg_name = "GetData" if data_mode is cp.DataMode.get\
                else "StoreData"

            # If in get_data mode, then send a GetData message to each Peer
            # that indicated data presence, one at a time, stopping upon first
            # success. Right now we will start at closest node, which might
            # make it harder to Sybil attack targeted data_ids.
            #TODO: Figure out if that is so, because otherwise we might not
            # want to grab from the closest for load balancing purposes.
            # Certainly future versions should have a more advanced algorithm
            # here that bases the decision on latency, tunnel depth, trust,
            # Etc.

            # If in store_data mode, then send the data to the closest willing
            # nodes that we found.

            if log.isEnabledFor(logging.INFO):
                log.info("Sending {} with {} tunnels still open."\
                    .format(msg_name, task_cntr.value))

            # Just FYI: There might be no tunnels open if we are connected to
            # everyone, or only immediate PeerS were closest.

            sent_data_request.value = 1 # Let tunnel process tasks know.

            # We have to process responses from three different cases:
            # 1. Peer reached through a tunnel.
            # 2. Immediate Peer that is also an open tunnel. (task running)
            # 3. Immediate Peer that is not an open tunnel.
            # The last case requires a new processing task as no task is
            # already running to handle it. Case 1 & 2 are handled by the
            # _process_find_node_relay(..) co-routine tasks. The last case can
            # only happen if there was no tunnel opened with that Peer. If the
            # tunnel got closed then we don't even use that immediate Peer so
            # it being closed won't be a case we have to handle. (We don't
            # reopen channels at this point.)

            for row in result_trie:
                if row is False:
                    # Row is ourself.
                    if data_mode is cp.DataMode.get:
                        data_present =\
                            yield from self._check_has_data(node_id)

                        if not data_present:
                            continue

                        log.info("We have the data; fetching.")

                        enc_data, data_l =\
                            yield from self._retrieve_data(node_id)

                        drmsg = cp.ChordDataResponse()
                        drmsg.data = enc_data
                        drmsg.original_size = data_l

                        r = yield from self._process_data_response(\
                            drmsg, None, None, data_rw)

                        if not r:
                            # Data was invalid somehow!
                            log.warning("Data from ourselves was invalid!")
                            continue

                        # Otherwise, break out of the loop as we've fetched the
                        # data.
                        break
                    else:
                        assert data_mode is cp.DataMode.store

                        will_store, need_pruning =\
                            yield from self._check_do_want_data(node_id)

                        if not will_store:
                            continue

                        log.info("We are choosing to additionally store the"\
                            " data locally.")

                        dmsg = cp.ChordStoreData()
                        dmsg.data = data
                        dmsg.data_id = node_id

                        r = yield from self._store_data(\
                            None, dmsg, need_pruning)

                        if not r:
                            log.info("We failed to store the data.")

                        # Store it still elsewhere if others want it as well.
                        continue

                if data_mode is cp.DataMode.get:
                    if not row.data_present:
                        # This node doesn't have our data.
                        continue
                else:
                    assert data_mode is cp.DataMode.store

                    if not row.will_store:
                        # The node may be close to id, but it says that it
                        # does not want to store the proposed data for whatever
                        # reason.
                        continue

                tun_meta = row.tun_meta
                if tun_meta and not tun_meta.queue:
                    # Peer is reached through a tunnel, but the tunnel is
                    # closed.
                    continue

                if log.isEnabledFor(logging.DEBUG):
                    log.debug("Sending {} to Peer [{}] and path [{}]."\
                        .format(msg_name, row.peer.address, row.path))

                if data_mode is cp.DataMode.get:
                    msg = cp.ChordGetData()
                else:
                    assert data_mode is cp.DataMode.store

                    msg = cp.ChordStoreData()
                    msg.data_id = node_id
                    msg.data = data

                if tun_meta:
                    # Then this is a Peer reached through a tunnel.
                    pkt = self._generate_relay_packets(\
                        row.path, msg.encode())
                    tun_meta.jobs += 1
                else:
                    # Then this is an immediate Peer.
                    pkt = msg.encode()

                    tun_meta = used_tunnels.get(row)

                    if tun_meta.task_running:
                        # Then this immediate Peer is an open tunnel and will
                        # be handled as described above for case #2.
                        tun_meta.jobs += 1
                    else:
                        # Then this immediate Peer is not an open tunnel and we
                        # will have to start a task to process its DataStored
                        # message.
                        asyncio.async(\
                            self._wait_for_data_stored(\
                                data_mode, row, tun_meta, query_cntr,\
                                done_all, data_rw),\
                            loop=self.loop)

                tun_meta.peer.protocol.write_channel_data(\
                    tun_meta.local_cid, pkt)

                query_cntr.value += 1

                if data_mode is cp.DataMode.get:
                    # We only send one at a time, stopping at success.
                    yield from done_all.wait()

                    if data_rw.data:
                        # If the data was read and validated successfully, then
                        # break out of the loop and clean up.
                        break
                    else:
                        # If the data was not validated correctly, then we ask
                        # the next Peer.
                        continue

                if query_cntr.value == max_concurrent_queries:
                    break

            if data_mode is cp.DataMode.store:
                if log.isEnabledFor(logging.INFO):
                    log.info("Sent StoreData to [{}] nodes."\
                        .format(query_cntr.value))

            if query_cntr.value:
                # query_cntr can be zero if no PeerS were tried.
                yield from done_all.wait()
                done_all.clear()

            assert query_cntr.value == 0

            if log.isEnabledFor(logging.INFO):
                log.info("Finished waiting for {} operations; now"\
                    " cleaning up.".format(msg_name))

        # Close everything now that we are done.
        tasks.clear()
        for tun_meta in used_tunnels.values():
            tasks.append(\
                tun_meta.peer.protocol.close_channel(tun_meta.local_cid))
        yield from asyncio.wait(tasks, loop=self.loop)

        if data_mode.value:
            if data_mode is cp.DataMode.store:
                # In store mode we don't return the peers to save CPU for now.
                return
            else:
                assert data_mode is cp.DataMode.get

                if not data_rw.data:
                    log.info("Failed to find the data!")

                return data_rw.data

        rnodes = [vpeer.peer for vpeer in result_trie if vpeer and vpeer.path]

        if log.isEnabledFor(logging.INFO):
            for vpeer in result_trie:
                if not vpeer or not vpeer.path:
                    continue
                log.info("Found closer Peer (address={})."\
                    .format(vpeer.peer.address))

        if log.isEnabledFor(logging.INFO):
            log.info("FindNode found [{}] Peers.".format(len(rnodes)))

        return rnodes

    def _generate_relay_packets(self, path, payload=None):
        "path: list of indexes."\
        "payload_msg: optional packet data to wrap."

        #TODO: MAYBE: replace embedded ChordRelay packets with
        # just one that has a 'path' field. Just more simple,
        # efficient and should be easy change. It means less work
        # for intermediate nodes that may want to examine the deepest
        # packet, in the case of data, in order to opportunistically
        # store the data. This might be a less good solution for
        # anonyminty, but it could be as easilty switched back if that
        # is true and a priority.

        #TODO: ChordRelay should be modified to allow a message payload instead
        # of the byte 'packet' payload. This way it can recursively call
        # encode() on the payloads that way appending data each iteration
        # instead of the inefficient way it does it now with inserting the
        # wrapping packet each iteration. This is an especially important
        # improvement now that a huge data packet is tacked on the end.

        pkt = None
        for idx in reversed(path):
            msg = cp.ChordRelay()
            msg.index = idx
            if pkt:
                msg.packets = [pkt]
            else:
                if payload:
                    msg.packets = [payload]
                else:
                    msg.packets = []
            pkt = msg.encode()

        return pkt

    @asyncio.coroutine
    def _send_find_node(self, vpeer, node_id, result_trie, tun_meta,\
            data_mode, far_peers_by_path):
        "Opens a channel and sends a 'root level' FIND_NODE to the passed"\
        " connected peer, adding results to the passed result_trie, and then"\
        " exiting. The channel is left open so that the caller may route to"\
        " those results through this 'root level' FIND_NODE peer."

        peer = vpeer.peer

        local_cid, queue =\
            yield from peer.protocol.open_channel("mpeer", True)
        if not queue:
            return

        msg = cp.ChordFindNode()
        msg.node_id = node_id
        msg.data_mode = data_mode

        if log.isEnabledFor(logging.DEBUG):
            log.debug("Sending root level FindNode msg to Peer (dbid=[{}])."\
                .format(peer.dbid))

        peer.protocol.write_channel_data(local_cid, msg.encode())

        pkt = yield from queue.get()
        if not pkt:
            return

        tun_meta.queue = queue
        tun_meta.local_cid = local_cid

        if data_mode.value:
            if data_mode is cp.DataMode.store:
                msg = cp.ChordStorageInterest(pkt)
                vpeer.will_store = msg.will_store

                pkt = yield from queue.get()
                if not pkt:
                    return
            elif data_mode is cp.DataMode.get:
                msg = cp.ChordDataPresence(pkt)
                vpeer.data_present = msg.data_present

                pkt = yield from queue.get()
                if not pkt:
                    return

        msg = cp.ChordPeerList(pkt)

        if log.isEnabledFor(logging.DEBUG):
            log.debug("Root level FindNode to Peer (id=[{}]) returned {}"\
                " PeerS.".format(peer.dbid, len(msg.peers)))

        idx = 0
        for rpeer in msg.peers:
            if log.isEnabledFor(logging.DEBUG):
                log.debug("Peer (dbid=[{}]) returned PeerList containing Peer"\
                    " (address=[{}]).".format(peer.dbid, rpeer.address))

            vpeer = VPeer(rpeer, [idx], tun_meta)

            key = bittrie.XorKey(node_id, rpeer.node_id)
            result_trie.setdefault(key, vpeer)
            if data_mode.value:
                far_peers_by_path.setdefault((idx,), vpeer)

            idx += 1

    @asyncio.coroutine
    def _process_find_node_relay(\
            self, node_id, tun_meta, query_cntr, done_all, task_cntr,\
            result_trie, data_mode, far_peers_by_path, sent_data_request,\
            data_rw):
        "This method processes an open tunnel's responses, processing the"\
        " incoming messages and appending the PeerS in those messages to the"\
        " result_trie. This method does not close any channel to the tunnel,"\
        " and does not stop processing and exit until the channel is closed"\
        " either by the Peer or by our side outside this method."

        assert type(sent_data_request) is Counter

        tun_meta.task_running = True

        try:
            r = yield from self.__process_find_node_relay(\
                node_id, tun_meta, query_cntr, done_all, task_cntr,\
                result_trie, data_mode, far_peers_by_path, sent_data_request,\
                data_rw)

            if not r:
                return
        except:
            log.exception("__process_find_node_relay(..)")

        if tun_meta.jobs:
            # This tunnel closed while there were still pending jobs, so
            # consider those jobs now completed and subtract them from the
            # count of ongoing jobs.
            query_cntr.value -= tun_meta.jobs
            if not query_cntr.value:
                done_all.set()
            tun_meta.jobs = 0

        # Mark tunnel as closed.
        tun_meta.queue = None
        tun_meta.task_running = False
        # Update counter of open tunnels.
        task_cntr.value -= 1

    @asyncio.coroutine
    def __process_find_node_relay(\
            self, node_id, tun_meta, query_cntr, done_all, task_cntr,\
            result_trie, data_mode, far_peers_by_path, sent_data_request,\
            data_rw):
        "Inner function for above call."
        while True:
            pkt = yield from tun_meta.queue.get()
            if not pkt:
                break

            if sent_data_request.value\
                    and cp.ChordMessage.parse_type(pkt) != cp.CHORD_MSG_RELAY:
                # This co-routine only expects unwrapped packets in the case
                # we have sent data and are waiting for an ack from the
                # immediate Peer.
                pkts = (pkt,)
                path = None
            else:
                if log.isEnabledFor(logging.DEBUG):
                    log.debug("Unwrapping ChordRelay packet.")
                pkts, path = self.unwrap_relay_packets(pkt, data_mode)

            pkt_type = cp.ChordMessage.parse_type(pkts[0])

            if data_mode.value and pkt_type != cp.CHORD_MSG_PEER_LIST:
                # Above pkt_type check is because a node that had no closer
                # PeerS for us and didn't have or want data will have closed
                # the channel and thus caused only an empty PeerList to be
                # sent to us.
                if sent_data_request.value:
                    if data_mode is cp.DataMode.get:
                        if pkt_type != cp.CHORD_MSG_DATA_RESPONSE:
                            # They are too late! We are only looking for
                            # DataResponse messages now.
                            continue

                        rmsg = cp.ChordDataResponse(pkts[0])

                        r = yield from self._process_data_response(\
                            rmsg, tun_meta, path, data_rw)

                        if not r:
                            # If the data was invalid, we will try from another
                            # Peer (or possibly tunnel).
                            query_cntr.value -= 1
                            assert not query_cntr.value
                            done_all.set()
                            continue
                    else:
                        assert data_mode is cp.DataMode.store

                        if pkt_type != cp.CHORD_MSG_DATA_STORED:
                            # They are too late! We are only looking for
                            # DataStored messages now.
                            continue
                        else:
                            if log.isEnabledFor(logging.DEBUG):
                                log.debug("Received DataStored message from"\
                                    " Peer (dbid={})."\
                                        .format(tun_meta.peer.dbid, path))

                    query_cntr.value -= 1
                    if not query_cntr.value:
                        done_all.set()
                        return False

                    continue

                if data_mode is cp.DataMode.get:
                    pmsg = cp.ChordDataPresence(pkts[0])
                    if pmsg.data_present:
                        rvpeer = far_peers_by_path.get(tuple(path))
                        if rvpeer is None:
                            #FIXME: Treat this as attack, Etc.
                            log.warning("Far node not found in dict for path"\
                                "[{}].".format(path))
                        else:
                            rvpeer.data_present = True
                else:
                    assert data_mode is cp.DataMode.store

                    imsg = cp.ChordStorageInterest(pkts[0])
                    if imsg.will_store:
                        rvpeer = far_peers_by_path.get(tuple(path))
                        if rvpeer is None:
                            #FIXME: Treat this as attack, Etc.
                            log.warning("Far node not found in dict for path"\
                                "[{}].".format(path))
                        else:
                            rvpeer.will_store = True

                pkt = pkts[1]
            else:
                pkt = pkts[0]

            pmsg = cp.ChordPeerList(pkt)

            log.info("Peer (id=[{}]) returned PeerList of size {}."\
                .format(tun_meta.peer.dbid, len(pmsg.peers)))

            # Add returned PeerS to result_trie.
            idx = 0
            for rpeer in pmsg.peers:
                end_path = tuple(path)
                end_path += (idx,)

                vpeer = VPeer(rpeer, end_path, tun_meta)

                key = bittrie.XorKey(node_id, rpeer.node_id)
                result_trie.setdefault(key, vpeer)

                if data_mode.value:
                    far_peers_by_path.setdefault(end_path, vpeer)

                idx += 1

            if not tun_meta.jobs:
                #FIXME: Should handle this as an attack and ignore them and
                # update tracking of hostility of the Peer AND tunnel.
                log.info(\
                    "Got extra result from tunnel (Peer.id={}, path=[{}])."\
                        .format(tun_meta.peer.dbid, path))
                continue

            tun_meta.jobs -= 1
            query_cntr.value -= 1
            if not query_cntr.value:
                done_all.set()

        return True

    def unwrap_relay_packets(self, pkt, data_mode):
        "Returns the inner most packet and the path stored in the relay"\
        " packets."

        path = []
        invalid = False

        while True:
            msg = cp.ChordRelay(pkt)
            path.append(msg.index)
            pkts = msg.packets

            if len(pkts) == 1:
                pkt = pkts[0]

                packet_type = cp.ChordMessage.parse_type(pkt)
                if packet_type == cp.CHORD_MSG_PEER_LIST\
                        or (data_mode is cp.DataMode.get\
                            and packet_type == cp.CHORD_MSG_DATA_RESPONSE)\
                        or (data_mode is cp.DataMode.store\
                            and packet_type == cp.CHORD_MSG_DATA_STORED):
                    break
                elif packet_type != cp.CHORD_MSG_RELAY:
                    log.warning("Unexpected packet_type [{}]; ignoring."\
                        .format(packet_type))
                    invalid = True
                    break
            elif len(pkts) > 1:
                # In data mode, PeerS return their storage intent, as well as a
                # list of their connected PeerS.
                if data_mode.value:
                    if data_mode is cp.DataMode.get\
                            and (cp.ChordMessage.parse_type(pkts[0])\
                                    != cp.CHORD_MSG_DATA_PRESENCE\
                                or cp.ChordMessage.parse_type(pkts[1])\
                                    != cp.CHORD_MSG_PEER_LIST):
                        invalid = True
                    elif data_mode is cp.DataMode.store\
                            and (cp.ChordMessage.parse_type(pkts[0])\
                                    != cp.CHORD_MSG_STORAGE_INTEREST\
                                or cp.ChordMessage.parse_type(pkts[1])\
                                    != cp.CHORD_MSG_PEER_LIST):
                        invalid = True
                else:
                    invalid = True

                # Break as we reached deepest packets.
                break
            else:
                # There should never be an empty relay packet embedded when
                # this method is called.
                invalid = True
                break

        if invalid:
            #FIXME: We should probably update the hostility tracking of both
            # the Peer and the tunnel Peer instead of just ignoring this
            # invalid state.
            log.warning("Unwrapping found invalid state.")

            pkts = []

            if data_mode is cp.DataMode.get:
                pkts.append(cp.ChordDataPresence().encode())
            elif data_mode is cp.DataMode.store:
                pkts.append(cp.ChordStorageInterest().encode())

            tpeerlist = cp.ChordPeerList()
            tpeerlist.peers = []
            pkts.append(tpeerlist.encode())

        if log.isEnabledFor(logging.DEBUG):
            log.debug("Unwrapped {} packets.".format(len(pkts)))

        return pkts, path

    @asyncio.coroutine
    def _wait_for_data_stored(self, data_mode, vpeer, tun_meta, query_cntr,\
            done_all, data_rw):
        "This is a coroutine that is used in data_mode and is started for"\
        " immediate PeerS that do not have a tunnel open (and thus a tunnel"\
        " coroutine already processing it."

        while True:
            pkt = yield from tun_meta.queue.get()
            if not pkt:
                break

            if data_mode is cp.DataMode.get:
                rmsg = cp.ChordDataResponse(pkt)

                r = yield from self._process_data_response(\
                    rmsg, tun_meta, None, data_rw)

                if not r:
                    # If the data was invalid, we will try from another
                    # Peer (or possibly tunnel).
                    query_cntr.value -= 1
                    assert not query_cntr.value
                    done_all.set()
                    continue
            else:
                assert data_mode is cp.DataMode.store

                msg = cp.ChordDataStored(pkt)

            break

        query_cntr.value -= 1
        if not query_cntr.value:
            done_all.set()

    @asyncio.coroutine
    def process_find_node_request(self, fnmsg, fndata, peer, queue, local_cid):
        "Process an incoming FindNode request."\
        " The channel will be closed before this method returns."

        pt = bittrie.BitTrie()

        for cpeer in self.engine.peers.values():
            if cpeer == peer:
                # Don't include asking peer.
                continue

            pt[bittrie.XorKey(fnmsg.node_id, cpeer.node_id)] = cpeer

        # We don't want to deal with further nodes than ourselves.
        pt[bittrie.XorKey(fnmsg.node_id, self.engine.node_id)] = True

        cnt = 3
        rlist = []

        for r in pt:
            if r is True:
                log.info("No more nodes closer than ourselves.")
                break

            if log.isEnabledFor(logging.DEBUG):
                log.debug("nn: {} FOUND: {:7} {:22} node_id=[{}] diff=[{}]"\
                    .format(self.engine.node.instance, r.dbid, r.address,\
                        hex_string(r.node_id),\
                        hex_string(\
                            self.engine.calc_raw_distance(\
                                r.node_id, fnmsg.node_id))))

            rlist.append(r)

            cnt -= 1
            if not cnt:
                break

        # Free memory? We no longer need this, and we may be tunneling for
        # some time.
        pt = None

        will_store = False
        need_pruning = False
        data_present = False
        if fnmsg.data_mode.value:
            # In for_data mode we respond with two packets.
            if fnmsg.data_mode is cp.DataMode.get:
                data_present = yield from self._check_has_data(fnmsg.node_id)
                pmsg = cp.ChordDataPresence()
                pmsg.data_present = data_present

                log.info("Writing DataPresence (data_present=[{}]) response."\
                    .format(data_present))

                peer.protocol.write_channel_data(local_cid, pmsg.encode())
            elif fnmsg.data_mode is cp.DataMode.store:
                will_store, need_pruning =\
                    yield from self._check_do_want_data(fnmsg.node_id)

                imsg = cp.ChordStorageInterest()
                imsg.will_store = will_store

                log.info("Writing StorageInterest (will_store=[{}]) response."\
                    .format(will_store))

                peer.protocol.write_channel_data(local_cid, imsg.encode())
            else:
                log.warning("Invalid data_mode ([{}])."\
                    .format(fnmsg.data_mode))

        if not rlist:
            log.info("No nodes closer than ourselves.")
            if not will_store and not data_present:
                yield from peer.protocol.close_channel(local_cid)
                return

        lmsg = cp.ChordPeerList()
        lmsg.peers = rlist

        log.info("Writing PeerList (size={}) response.".format(len(rlist)))
        peer.protocol.write_channel_data(local_cid, lmsg.encode())

        rlist = [TunnelMeta(rpeer) for rpeer in rlist]

        tun_cntr = Counter(len(rlist))

        while True:
            pkt = yield from queue.get()
            if not pkt:
                # If the requestor channel closes, or the connection went down,
                # then abort the FindNode completely.
                yield from self._close_tunnels(rlist)
                return

            if not tun_cntr.value and not will_store and not data_present:
                # If all the tunnels were closed and we aren't waiting for
                # data, then we clean up and exit.
                yield from self._close_tunnels(rlist)
                yield from peer.protocol.close_channel(local_cid)
                return

            packet_type = cp.ChordMessage.parse_type(pkt)
            if will_store and packet_type == cp.CHORD_MSG_STORE_DATA:
                if log.isEnabledFor(logging.INFO):
                    log.info("Received ChordStoreData packet, storing.")

                rmsg = cp.ChordStoreData(pkt)

                r = yield from self._store_data(peer, rmsg, need_pruning)

                dsmsg = cp.ChordDataStored()
                dsmsg.stored = r

                peer.protocol.write_channel_data(local_cid, dsmsg.encode())
                continue
            elif data_present and packet_type == cp.CHORD_MSG_GET_DATA:
                if log.isEnabledFor(logging.INFO):
                    log.info("Received ChordGetData packet, fetching.")

                data, data_l = yield from self._retrieve_data(fnmsg.node_id)

                drmsg = cp.ChordDataResponse()
                drmsg.data = data
                drmsg.original_size = data_l

                peer.protocol.write_channel_data(local_cid, drmsg.encode())

                # After we return the data, since we are honest, there is no
                # point in the requesting node asking for data from one of our
                # immediate PeerS through us as a tunnel, so we clean up and
                # exit.
                yield from self._close_tunnels(rlist)
                yield from peer.protocol.close_channel(local_cid)
                return
            else:
                rmsg = cp.ChordRelay(pkt)

            if log.isEnabledFor(logging.DEBUG):
                log.debug("Processing request from Peer (id=[{}]) for index"\
                    " [{}].".format(peer.dbid, rmsg.index))

            tun_meta = rlist[rmsg.index]

            if not tun_meta.queue:
                # First packet to a yet to be utilized Peer should be empty,
                # which instructs us to open the tunnel and forward it the root
                # FindNode packet that started this process.
                if len(rmsg.packets):
                    log.warning("Peer sent invalid packet (not empty but"\
                        " tunnel not yet opened) for index [{}]; skipping."\
                        .format(rmsg.index))
                    continue

                tun_meta.jobs = asyncio.Queue()
                asyncio.async(\
                    self._process_find_node_tunnel(\
                        peer, local_cid, rmsg.index, tun_meta, tun_cntr,\
                        fnmsg.data_mode),\
                    loop=self.loop)
                yield from tun_meta.jobs.put(fndata)
            elif tun_meta.jobs:
                if not len(rmsg.packets):
                    log.warning("Peer [{}] sent additional empty relay packet"\
                        " for tunnel [{}]; skipping."\
                            .format(peer.dbid, rmsg.index))
                    continue
                if len(rmsg.packets) > 1:
                    log.warning("Peer [{}] sent relay packet with more than"\
                        " one embedded packet for tunnel [{}]; skipping."\
                            .format(peer.dbid, rmsg.index))
                    continue

                e_pkt = rmsg.packets[0]

                if cp.ChordMessage.parse_type(e_pkt) != cp.CHORD_MSG_RELAY:
                    if not fnmsg.data_mode.value:
                        log.warning("Peer [{}] sent a non-empty relay packet"\
                            " with other than a relay packet embedded for"\
                            " tunnel [{}]; skipping."\
                                .format(peer.dbid, rmsg.index))
                        continue
                    # else: It is likely a {Get,Store}Data message, which is
                    # ok.

                # If all good, tell tunnel process to forward embedded packet.
                yield from tun_meta.jobs.put(e_pkt)
            else:
                if log.isEnabledFor(logging.INFO):
                    log.info("Skipping request for disconnected tunnel [{}]."\
                        .format(rmsg.index))

                yield from self._signal_find_node_tunnel_closed(\
                    peer, local_cid, rmsg.index, 1)

    @asyncio.coroutine
    def _process_find_node_tunnel(\
            self, rpeer, rlocal_cid, index, tun_meta, tun_cntr, data_mode):
        assert type(rpeer) is mnpeer.Peer

        "Start a tunnel to the Peer in tun_meta by opening a channel and then"
        " passing all packets put into the tun_meta.jobs queue to the Peer."
        " Another coroutine is started to process the responses and send them"
        " back to the Peer passed in rpeer."

        if log.isEnabledFor(logging.INFO):
            log.info("Opening tunnel [{}] to Peer (id=[{}]) for Peer(id=[{}])."\
                .format(index, tun_meta.peer.dbid, rpeer.dbid))

        tun_meta.local_cid, tun_meta.queue =\
            yield from tun_meta.peer.protocol.open_channel("mpeer", True)
        if not tun_meta.queue:
            tun_cntr.value -= 1

            job_cnt = tun_meta.jobs.qsize()
            tun_meta.jobs = None
            assert job_cnt == 1

            yield from\
                self._signal_find_node_tunnel_closed(\
                    rpeer, rlocal_cid, index, job_cnt)
            return

        req_cntr = Counter(0)

        asyncio.async(\
            self._process_find_node_tunnel_responses(\
                rpeer, rlocal_cid, index, tun_meta, req_cntr, data_mode),
            loop=self.loop)

        jobs = tun_meta.jobs
        while True:
            pkt = yield from jobs.get()
            if not pkt or not tun_meta.jobs:
                tun_cntr.value -= 1
                yield from\
                    tun_meta.peer.protocol.close_channel(tun_meta.local_cid)
                return

            if log.isEnabledFor(logging.DEBUG):
                log.debug("Relaying request (index={}) from Peer (id=[{}])"\
                    " to Peer (id=[{}])."\
                    .format(index, rpeer.dbid, tun_meta.peer.dbid))

            tun_meta.peer.protocol.write_channel_data(tun_meta.local_cid, pkt)

            req_cntr.value += 1

    @asyncio.coroutine
    def _process_find_node_tunnel_responses(\
            self, rpeer, rlocal_cid, index, tun_meta, req_cntr, data_mode):
        "Process the responses from a tunnel and relay them back to rpeer."

        while True:
            pkt = yield from tun_meta.queue.get()
            if not tun_meta.jobs:
                return
            if not pkt:
                break

            pkt2 = None
            if data_mode.value:
                pkt_type = cp.ChordMessage.parse_type(pkt)

                # These exceptions are for packets directly from the immediate
                # Peer, as opposed to relay packets containing responses from
                # PeerS being accessed through the immediate Peer acting as a
                # tunnel.

                if data_mode is cp.DataMode.get\
                        and pkt_type == cp.CHORD_MSG_DATA_RESPONSE:
                    #TODO: Verify the data matches the key before relaying.
                    # Relay the DataResponse from immediate Peer.
                    pass
                elif data_mode is cp.DataMode.store\
                        and pkt_type == cp.CHORD_MSG_DATA_STORED:
                    # Relay the DataStored from immediate Peer.
                    pass
                elif pkt_type != cp.CHORD_MSG_RELAY:
                    # First two packets from a newly opened tunnel in data_mode
                    # mode will be the DataPresence or StorageInterest\
                    # and PeerList packet.
                    pkt2 = yield from tun_meta.queue.get()
                    if not tun_meta.jobs:
                        return
                    if not pkt2:
                        break

            if log.isEnabledFor(logging.DEBUG):
                log.debug("Relaying response (index={}) from Peer (id=[{}])"\
                    " to Peer (id=[{}])."\
                    .format(index, tun_meta.peer.dbid, rpeer.dbid))

            msg = cp.ChordRelay()
            msg.index = index
            if pkt2:
                msg.packets = [pkt, pkt2]
            else:
                msg.packets = [pkt]

            rpeer.protocol.write_channel_data(rlocal_cid, msg.encode())

            req_cntr.value -= 1

        outstanding = tun_meta.jobs.qsize() + req_cntr.value
        yield from\
            self._signal_find_node_tunnel_closed(\
                rpeer, rlocal_cid, index, outstanding)

        jobs = tun_meta.jobs
        tun_meta.jobs = None
        yield from jobs.put(None)

    @asyncio.coroutine
    def _signal_find_node_tunnel_closed(self, rpeer, rlocal_cid, index, cnt):
        rmsg = cp.ChordRelay()
        rmsg.index = index
        rmsg.packets = [EMPTY_PEER_LIST_PACKET]
        pkt = rmsg.encode()

        for _ in range(cnt):
            # Signal the query finished with no results.
            rpeer.protocol.write_channel_data(rlocal_cid, pkt)

    @asyncio.coroutine
    def _close_tunnels(self, meta_list):
        for tun_meta in meta_list:
            if not tun_meta.queue:
                continue
            if tun_meta.jobs:
                jobs = tun_meta.jobs
                tun_meta.jobs = None
                yield from jobs.put(None)

            yield from\
                tun_meta.peer.protocol.close_channel(tun_meta.local_cid)

    @asyncio.coroutine
    def _check_has_data(self, data_id):
        def dbcall():
            with self.engine.node.db.open_session() as sess:
                q = sess.query(func.count("*"))
                q = q.filter(DataBlock.data_id == data_id)

                if q.scalar() > 0:
                    return True
                else:
                    return False

        return (yield from self.loop.run_in_executor(None, dbcall))

    @asyncio.coroutine
    def _check_do_want_data(self, data_id):
        "Checks if we have space to store, and if not if we have enough data"\
        " that is further in distance thus having enough space to free."\
        "returns: will_store, need_pruning"

        if self.engine.node.datastore_size\
                < self.engine.node.datastore_max_size:
            return True, False

        distance = self.engine.calc_raw_distance(data_id, self.engine.node_id)

        # If there is space contention, then we do a more complex algorithm
        # in order to see if we want to store it.
        def dbcall():
            with self.engine.node.db.open_session() as sess:
                # We don't worry about inaccuracy caused by padding for now.

                q = sess.query(DataBlock.original_size)\
                    .filter(DataBlock.distance > distance)\
                    .order_by(DataBlock.distance.desc())

                freeable_space = 0
                for original_size in page_query(q):
                    freeable_space += original_size

                    if freeable_space >= mnnode.MAX_DATA_BLOCK_SIZE:
                        return True

                assert freeable_space < mnnode.MAX_DATA_BLOCK_SIZE
                return False

        return (yield from self.loop.run_in_executor(None, dbcall)), True

    @asyncio.coroutine
    def _process_data_response(self, data_response, tun_meta, path, data_rw):
        "Processes the DataResponse packet, storing the decrypted data into"\
        " data_rw if it matches the original key. Returns True on success,"\
        " False otherwise."

        if log.isEnabledFor(logging.INFO):
            peer_dbid = tun_meta.peer.dbid if tun_meta else "<self>"
            log.info("Received DataResponse from Peer [{}] and path [{}]."\
                .format(peer_dbid, path))

        def threadcall():
            data = enc.decrypt_data_block(data_response.data, data_rw.data_key)

            # Truncate the data to exclude the cipher padding.
            data = data[:data_response.original_size]

            # Verify that the decrypted data matches the original hash of it.
            data_key = enc.generate_ID(data)

            if data_key == data_rw.data_key:
                data_rw.data = data
                return True
            else:
                return False

        return (yield from self.loop.run_in_executor(None, threadcall))

    @asyncio.coroutine
    def _retrieve_data(self, data_id):
        "Retrieve data for data_id from the file system (and meta data from"\
        " the database."\
        "returns: data, original_size"\
        "   original_size is the size of the data before it was encrypted."

        def dbcall():
            with self.engine.node.db.open_session() as sess:
                data_block = sess.query(DataBlock).filter(\
                    DataBlock.data_id == data_id).first()

                if not data_block:
                    return None

                sess.expunge(data_block)

                return data_block

        data_block = yield from self.loop.run_in_executor(None, dbcall)

        if not data_block:
            return None, None

        def iocall():
            data_file = open(
                self.engine.node.data_block_file_path\
                    .format(self.engine.node.instance, data_block.id),
                "rb")

            return data_file.read()

        enc_data = yield from self.loop.run_in_executor(None, iocall)

        return enc_data, data_block.original_size

    @asyncio.coroutine
    def _store_data(self, peer, dmsg, need_pruning):
        "Store the data block on disk and meta in the database. Returns True"
        " if the data was stored, False otherwise."

        #TODO: I now realize that this whole method should probably run in a
        # separate thread that is passed to run_in_executor(..), instead of
        # breaking it up into many such calls. Just for efficiency and since
        # there is probably no reason not to.
        #FIXME: This code needs to be fixed to use an additional table,
        # something like DataBlockJournal, which tracks pending deletes or
        # creations, thus ensuring the filesystem is kept in sync, even if
        # crashes, Etc.

        peer_dbid = peer.dbid if peer else "<self>"

        data = dmsg.data

        data_key = enc.generate_ID(data)
        data_id = enc.generate_ID(data_key)

        if data_id != dmsg.data_id:
            log.warning("Peer (dbid=[{}]) sent a data_id that didn't match"\
                " the data!".format(peer_dbid))

        distance = self.engine.calc_raw_distance(data_id, self.engine.node_id)
        original_size = len(data)

        def dbcall():
            with self.engine.node.db.open_session() as sess:
                self.engine.node.db.lock_table(sess, DataBlock)

                q = sess.query(func.count("*"))
                q = q.filter(DataBlock.data_id == data_id)

                if q.scalar() > 0:
                    # We already have this block.
                    return None, None

                if need_pruning:
                    freeable_space = 0
                    blocks_to_prune = []

                    q = sess.query(DataBlock.id, DataBlock.original_size)\
                        .filter(DataBlock.distance > distance)\
                        .order_by(DataBlock.distance.desc())

                    for block in page_query(q):
                        freeable_space += block[1]
                        blocks_to_prune.append(block[0])

                        if freeable_space >= original_size:
                            break

                    if freeable_space < original_size:
                        return False, None

                    if log.isEnabledFor(logging.INFO):
                        log.info("Pruning {} blocks to make room."\
                            .format(len(blocks_to_prune)))

                    for anid in blocks_to_prune:
                        sess.query(DataBlock)\
                            .filter(DataBlock.id == anid)\
                            .delete(synchronize_session=False)

                data_block = DataBlock()
                data_block.data_id = data_id
                data_block.distance = distance
                data_block.original_size = original_size
                data_block.insert_timestamp = datetime.today()

                sess.add(data_block)

                # Rule: only update this NodeState row when holding a lock on
                # the DataBlock table.
                node_state = sess.query(NodeState)\
                    .filter(NodeState.key == mnnode.NSK_DATASTORE_SIZE)\
                    .first()

                if not node_state:
                    node_state = NodeState()
                    node_state.key = mnnode.NSK_DATASTORE_SIZE
                    node_state.value = 0
                    sess.add(node_state)

                size_diff = original_size

                if need_pruning:
                    size_diff -= freeable_space

                node_state.value += size_diff

                sess.commit()

                if need_pruning:
                    for andid in blocks_to_prune:
                        os.remove(self.engine.node.data_block_file_path\
                            .format(self.engine.node.instance, anid))

                return data_block.id, size_diff

        data_block_id, size_diff =\
            yield from self.loop.run_in_executor(None, dbcall)

        if not data_block_id:
            if log.isEnabledFor(logging.INFO):
                if data_block_id is False:
                    log.info("Not storing block we said we would as we"\
                        " can won't free up enough space for it. (Some"\
                        " other block upload must have beaten this one to"\
                        " us.")
                else:
                    log.info("Not storing data that we already have"\
                        " (data_id=[{}])."\
                        .format(hex_string(data_id)))
            return False

        self.engine.node.datastore_size += size_diff

        try:
            if log.isEnabledFor(logging.INFO):
                log.info("Encrypting [{}] bytes of data.".format(len(data)))

            #TODO: If not too much a performance cost: Hash encrypted data
            # block and store hash in the db so we can verify it didn't become
            # corrupted on the filesystem. This is because we will be penalized
            # by the network if we give invalid data when asked for.
            #NOTE: Actually, it should be fine as we can do it in another
            # thread and thus not impact our eventloop thread. We can do it
            # concurrently with encryption!

            # PyCrypto works in blocks, so extra than round block size goes
            # into enc_data_remainder.
            def threadcall():
                return enc.encrypt_data_block(data, data_key)

            enc_data, enc_data_remainder\
                = yield from self.loop.run_in_executor(None, threadcall)

            if log.isEnabledFor(logging.INFO):
                tlen = len(enc_data)
                if enc_data_remainder:
                    tlen += len(enc_data_remainder)
                log.info("Storing [{}] bytes of data.".format(tlen))

            def iocall():
                new_file = open(
                    self.engine.node.data_block_file_path\
                        .format(self.engine.node.instance, data_block_id),
                    "wb")

                new_file.write(enc_data)
                if enc_data_remainder:
                    new_file.write(enc_data_remainder)

            yield from self.loop.run_in_executor(None, iocall)

            if log.isEnabledFor(logging.INFO):
                log.info("Stored data for data_id=[{}] as [{}.blk]."\
                    .format(hex_string(data_id), data_block_id))

            return True
        except Exception as e:
            log.exception("encrypt/write_to_disk")

            log.warning("There was an exception attempting to store the data"\
                " on disk.")

            def dbcall():
                with self.engine.node.db.open_session() as sess:
                    self.engine.node.db.lock_table(sess, DataBlock)

                    sess.query(DataBlock)\
                        .filter(DataBlock.id == data_block_id)\
                        .delete(synchronize_session=False)

                    # Rule: only update this NodeState row when holding a lock
                    # on the DataBlock table.
                    node_state = sess.query(NodeState)\
                        .filter(NodeState.key == mnnode.NSK_DATASTORE_SIZE)\
                        .first()

                    node_state.value -= original_size

                    sess.commit()

            yield from self.loop.run_in_executor(None, dbcall)

            self.engine.node.datastore_size -= original_size

            def iocall():
                os.remove(self.engine.node.data_block_file_path\
                    .format(self.engine.node.instance, data_block_id))

            try:
                yield from self.loop.run_in_executor(None, iocall)
            except Exception:
                log.exception("os.remove(..)")
                pass

            return False
