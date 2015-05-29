import llog

import asyncio
import cgi
from http.server import BaseHTTPRequestHandler, HTTPServer
import logging
from socketserver import ThreadingMixIn
from threading import Event

import base58
import enc
from mutil import hex_string
import rsakey

log = logging.getLogger(__name__)

host = "localhost"
port = 4251

node = None
server = None

upload_page_content = None
static_upload_page_content = None
static_upload_page_content_id = None

class DataResponseWrapper(object):
    def __init__(self):
        self.data = None
        self.data_key = None

        self.is_done = Event()

        self.exception = None
        self.timed_out = False

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    pass

class MaalstroomHandler(BaseHTTPRequestHandler):
    def __init__(self, a, b, c):
        super().__init__(a, b, c)

        self.protocol_version = "HTTP/1.1"

    def do_GET(self):
        rpath = self.path[1:]
        if rpath[-1] == '/':
            rpath = rpath[:-1]

        s_upload = "upload"
        if rpath.startswith(s_upload):
            if rpath.startswith("upload/generate"):
                priv_key =\
                    base58.encode(\
                        rsakey.RsaKey.generate(bits=4096)._encode_key())

                self.send_response(307)
                self.send_header("Location", "{}".format(priv_key))
                self.end_headers()
                return

            if self.headers["If-None-Match"] == static_upload_page_content_id:
                self.send_response(304)
                self.send_header("ETag", static_upload_page_content_id)
                self.end_headers()
                return

            if len(rpath) == len(s_upload):
                content = static_upload_page_content
                content_id = static_upload_page_content_id
            else:
                content =\
                    upload_page_content.replace(\
                        b"${PRIVATE_KEY}",\
                        rpath[len(s_upload)+1:].encode())
                content =\
                    content.replace(\
                        b"${UPDATEABLE_KEY_MODE_DISPLAY}",\
                        b"")
                content =\
                    content.replace(\
                        b"${STATIC_MODE_DISPLAY}",\
                        b"display: none")

                content_id = enc.generate_ID(content)

            self.send_response(200)
            self.send_header("Content-Length", len(content))
            self.send_header("Cache-Control", "public")
            self.send_header("ETag", content_id)
            self.end_headers()
            self.wfile.write(content)
            return

        if self.headers["If-None-Match"] == rpath:
            self.send_response(304)
            self.send_header("ETag", rpath)
            self.end_headers()
            return

        if log.isEnabledFor(logging.INFO):
            log.info("rpath=[{}].".format(rpath))

        error = False
        try:
            if len(rpath) == 128:
                data_key = bytes.fromhex(rpath)
            elif len(rpath) == 88 + 4 and rpath.startswith("get/"):
                data_key = base58.decode(rpath[4:])

                hex_key = hex_string(data_key)

                message = ("<a href=\"morphis://{}\">{}</a>\n{}"\
                    .format(hex_key, hex_key, hex_key))\
                        .encode()

                self.send_response(301)
                self.send_header("Location", "morphis://{}".format(hex_key))
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", len(message))
                self.end_headers()

                self.wfile.write(message)
                return
            else:
                error = True
                log.warning("Invalid request: [{}].".format(rpath))
        except:
            error = True
            log.exception("decode")

        if error:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"400 Bad Request.")
            return

        data_rw = DataResponseWrapper()

        node.loop.call_soon_threadsafe(\
            asyncio.async, _send_get_data(data_key, data_rw))

        data_rw.is_done.wait()

        if data_rw.data:
            self.send_response(200)
            if data_rw.data[0] == 0xFF and data_rw.data[1] == 0xD8:
                self.send_header("Content-Type", "image/jpg")
            elif data_rw.data[0] == 0x89 and data_rw.data[1:4] == b"PNG":
                self.send_header("Content-Type", "image/png")
            elif data_rw.data[:5] == b"GIF89":
                self.send_header("Content-Type", "image/gif")
            else:
                self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", len(data_rw.data))
            self.send_header("Cache-Control", "public")
            self.send_header("ETag", rpath)
            self.end_headers()

            self.wfile.write(data_rw.data)
        else:
            self.handle_error(data_rw)

    def do_POST(self):
        log.info(self.headers)

        if self.headers["Content-Type"] == "application/x-www-form-urlencoded":
            data = self.rfile.read(int(self.headers["Content-Length"]))
        else:
            form = cgi.FieldStorage(\
                fp=self.rfile,\
                headers=self.headers,\
                environ={\
                    "REQUEST_METHOD": "POST",\
                    "CONTENT_TYPE": self.headers["Content-Type"]})


            if log.isEnabledFor(logging.DEBUG):
                log.debug("form=[{}].".format(form))

            formelement = form["fileToUpload"]
            filename = formelement.filename
            data = formelement.file.read()

            if log.isEnabledFor(logging.INFO):
                log.info("filename=[{}].".format(filename))

            try:
                privatekey = form["privateKey"].value

                if privatekey == "${PRIVATE_KEY}":
                    raise KeyError()

                if log.isEnabledFor(logging.INFO):
                    log.info("privatekey=[{}].".format(privatekey))

                privatekey = base58.decode(privatekey)

                privatekey = rsakey.RsaKey(privdata=privatekey)

                path = form["path"].value
                version = form["version"].value
            except KeyError:
                privatekey = None

        if log.isEnabledFor(logging.DEBUG):
            log.debug("data=[{}].".format(data))

        data_rw = DataResponseWrapper()

        if privatekey:
            node.loop.call_soon_threadsafe(\
                asyncio.async, _send_store_data(\
                    data, data_rw, privatekey, path, version))
        else:
            node.loop.call_soon_threadsafe(\
                asyncio.async, _send_store_data(data, data_rw))

        data_rw.is_done.wait()

        if data_rw.data_key:
            hex_key = hex_string(data_rw.data_key)
            message = "<a href=\"morphis://{}\">perma link</a>\n{}\n{}"\
                .format(hex_key, hex_key, base58.encode(data_rw.data_key))

            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", len(message))
            self.end_headers()

            self.wfile.write(bytes(message, "UTF-8"))
        else:
            self.handle_error(data_rw)

    def handle_error(self, data_rw):
        if data_rw.exception:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"500 Internal Server Error.")
        elif data_rw.timed_out:
            self.send_response(408)
            self.end_headers()
            self.wfile.write(b"408 Request Timeout.")
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"404 Not Found.")

@asyncio.coroutine
def _send_get_data(data_key, data_rw):
    try:
        future = asyncio.async(\
            node.chord_engine.tasks.send_get_data(data_key),\
            loop=node.loop)

        yield from asyncio.wait_for(future, 15.0, loop=node.loop)

        data_rw.data = future.result()
    except asyncio.TimeoutError:
        data_rw.timed_out = True
    except:
        log.exception("send_get_data()")
        data_rw.exception = True

    data_rw.is_done.set()

@asyncio.coroutine
def _send_store_data(data, data_rw, privatekey=None, path=None, version=None):
    try:
        def key_callback(data_key):
            data_rw.data_key = data_key

        if privatekey:
            future = asyncio.async(\
                node.chord_engine.tasks.send_store_updateable_key(\
                    data, privatekey, path, version, key_callback),\
                loop=node.loop)
        else:
            future = asyncio.async(\
                node.chord_engine.tasks.send_store_data(data, key_callback),\
                loop=node.loop)

        yield from asyncio.wait_for(future, 30.0, loop=node.loop)
    except asyncio.TimeoutError:
        data_rw.timed_out = True
    except:
        log.exception("send_store_data()")
        data_rw.exception = True

    data_rw.is_done.set()

@asyncio.coroutine
def start_maalstroom_server(the_node):
    global node, server

    if node:
        #TODO: Handle this better, but for now this is how we only start one
        # maalstroom process even when running in multi-instance test mode.
        return

    node = the_node

    log.info("Starting Maalstroom server instance.")

    server = ThreadedHTTPServer((host, port), MaalstroomHandler)

    def threadcall():
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass

        server.server_close()

    node.loop.run_in_executor(None, threadcall)

def shutdown():
    if not server:
        return

    log.info("Shutting down Maalstroom server instance.")
    server.server_close()
    log.info("Mallstroom server instance stopped.")

def set_upload_page(filepath):
    global upload_page_content

    upf = open(filepath, "rb")
    _set_upload_page(upf.read())

def _set_upload_page(content):
    global static_upload_page_content, static_upload_page_content_id,\
        upload_page_content

    static_upload_page_content =\
        content.replace(\
            b"${UPDATEABLE_KEY_MODE_DISPLAY}",\
            b"display: none")
    static_upload_page_content=\
        static_upload_page_content.replace(\
            b"${STATIC_MODE_DISPLAY}",\
            b"")

    static_upload_page_content_id =\
        hex_string(enc.generate_ID(static_upload_page_content))

    upload_page_content = content

_set_upload_page(b'<html><head><title>Morphis Maalstroom Upload</title></head><body><p>Select the file to upload below:</p><form action="upload" method="post" enctype="multipart/form-data"><input type="file" name="fileToUpload" id="fileToUpload"/><br/><div style="${UPDATEABLE_KEY_MODE_DISPLAY}"><br/><label for="privateKey">Private Key</label><textarea name="privateKey" id="privateKey" rows="5" cols="80">${PRIVATE_KEY}</textarea><br/><label for="path">Path</label><input type="textfield" name="path"/><br/><label for="version">Version</label><input type="textfield" name="version"/></div><input type="submit" value="Upload File" name="submit"/></form><p style="${STATIC_MODE_DISPLAY}"><a href="morphis://upload/generate">switch to updateable key mode</a></p></body></html>')
