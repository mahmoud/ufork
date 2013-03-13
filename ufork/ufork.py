import os
import sys
import time
import socket
from multiprocessing import cpu_count
import threading
import code
import signal
from random import seed #re-seed random number generator post-fork
from collections import deque

TIMEOUT = 10.0

class Worker(object):
    def __init__(self, post_fork, child_pre_exit=lambda: None, sleep=None):
        self.post_fork = post_fork
        self.child_pre_exit = child_pre_exit
        self.sleep = sleep or time.sleep
        self.stopping = False
        self.sock = None
        self.pid = None
        self.last_update = time.time()

    def fork_and_run(self):
        parent, child = socket.socketpair()
        ppid = os.getpid()
        pid = os.fork()
        if pid: #in parent fork
            self.pid = pid
            self.sock = parent
            return
        #in child fork
        self.child_close_fds()
        sys.stdout = SockFile(child)
        sys.stderr = SockFile(child)
        os.close(0) #just close stdin for now so it doesnt mess up repl
        seed() #re-seed random number generator post-fork

        self.post_fork()

        try:
            while not self.stopping:
                try:
                    os.kill(ppid, 0) #kill 0 sends no signal, but checks that process exists
                except OSError as e:
                    print "caught exception5", e
                    break
                child.send('\0')
                self.sleep(1.0)
        except Exception as e:
            print "caught exception4", e
            raise
        finally:
            self.child_pre_exit()
        sys.exit(0)

    def parent_check(self):
        try:
            data = self.sock.recv(4096, socket.MSG_DONTWAIT)
        except socket.error:
            pass
        else:
            self.last_update = time.time()
            data = data.replace('\0', '')
            if data:
                print self.pid,':',data
        try: #check that process still exists
            os.kill(self.pid, 0)
        except OSError as e:
            print "caught exception1", e
            return False
        if time.time() - self.last_update > TIMEOUT:
            self.parent_kill()
            return False
        return True

    def parent_kill(self):
        try: #kill if proc still alive
            os.kill(self.pid, signal.SIGKILL)
        except OSError as e:
            print "caught exception2", e
            pass

    def child_close_fds(self):
        'close fds in the child after forking'
        pass #TODO -- figure out which should and shouldn't be closed

    def __repr__(self):
        return "ufork.Worker<pid="+str(self.pid)+">"

#SIGINT and SIGTERM mean shutdown cleanly

class Arbiter(object):
    def __init__(self, post_fork, child_pre_exit=None, size=None, sleep=None):
        self.post_fork = post_fork
        self.child_pre_exit = child_pre_exit
        if size is None:
            size = 2 * cpu_count() + 1
        self.size = size
        self.sleep = sleep or time.sleep
        global LAST_ARBITER
        LAST_ARBITER = self #for testing/debugging, a hook to get a global pointer

    def spawn_daemon(self):
        'causes run to be executed in a newly spawned daemon process'
        open('out.txt', 'a').close() #TODO: configurable output file
        if not os.fork():
            os.setsid() #create anew session (?) TODO: read up on this
            if os.fork(): #TODO: is setsid + double fork needed?
                os._exit(0)
            fd = os.open('out.txt', os.O_RDWR)
            os.close(0)
            os.dup2(fd, 1)
            os.dup2(fd, 2)
            self.run(False)

    def run(self, repl=True):
        workers = self.workers = set() #for efficient removal
        if repl:
            self.stdin_handler = StdinHandler(self)
            self.stdin_handler.start()
        self.stopping = False #for manual stopping
        dead_workers = self.dead_workers = deque()
        try:
            while not self.stopping:
                #spawn additional workers as needed
                for i in range(self.size - len(workers)):
                    worker = Worker(self.post_fork, self.child_pre_exit, self.sleep)
                    worker.fork_and_run()
                    workers.add(worker)
                #check for heartbeats from workers
                dead = set()
                for worker in workers:
                    if not worker.parent_check():
                        dead.add(worker)
                workers = workers - dead
                try: #reap dead workers
                    res = os.waitpid(-1, os.WNOHANG)
                    while res != (0,0):
                        dead_workers.append(res)
                        res = os.waitpid(-1, os.WNOHANG)
                except OSError as e:
                    print "caught exception3", e
                    pass #possible to get Errno 10: No child processes
                time.sleep(1.0)
        finally:
            for worker in workers:
                worker.parent_kill()
            self.stdin_handler.stop()

class SockFile(object):
    def __init__(self, sock):
        self.sock = sock

    def write(self, data):
        try:
            self.sock.send(data, socket.MSG_DONTWAIT)
        except socket.error:
            pass #TODO: something smarter

    #TODO: more file-functions as needed

class StdinHandler(object):
    'provides command-line interaction for Arbiter'
    def __init__(self, arbiter):
        self.arbiter = arbiter
        self.stopping = False
        self.read_thread = None
        context = dict(globals())
        context['arbiter'] = self.arbiter
        self.console = code.InteractiveConsole(context)

    def _interact(self):
        sys.stdout.flush()
        print '' #newline on startup to clear prompt
        while not self.stopping:
            inp = self.console.raw_input('ufork>> ')
            self.console.runsource(inp)
        print '' #newline after done to clear prompt
        sys.stdout.flush()

    def start(self):
        if self.stopping:
            raise Exception("StdinHandler is not restartable")
        self.read_thread = threading.Thread(target=self._interact)
        self.read_thread.daemon = True
        self.read_thread.start()

    def stop(self):
        self.stopping = True

try:
    import gevent
except:
    pass #gevent worker not defined
else:
    import gevent.pywsgi 
    import gevent.socket

    def serve_wsgi_gevent(wsgi, address, stop_timeout=30):
        sock = gevent.socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(address)
        sock.listen(128) #TODO: what value?
        server = gevent.pywsgi.WSGIServer(sock, wsgi)
        server.stop_timeout = stop_timeout
        arbiter = Arbiter(post_fork=server.start, child_pre_exit=server.stop, sleep=gevent.sleep)
        arbiter.run()

def serve_wsgiref_thread(wsgi, host, port):
    'probably not suitable for production use; example of threaded server'
    import wsgiref.simple_server
    httpd = wsgiref.simple_server.make_server(host, port, wsgi)
    httpd.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    def start_server():
        server_thread = threading.Thread(target=httpd.serve_forever)
        server_thread.daemon=True
        server_thread.start()
    arbiter = Arbiter(post_fork=start_server, child_pre_exit=httpd.shutdown)
    arbiter.run()

LAST_ARBITER = None
