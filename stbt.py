import argparse
import ConfigParser
import errno
import os
import re
import socket
import sys
import time


class ArgvHider:
    """ For use with 'with' statement:  Unsets argv and resets it.

    This is used because otherwise gst-python will exit if '-h', '--help', '-v'
    or '--version' command line arguments are given.
    """
    def __enter__(self):
        self.argv = sys.argv[:]
        del sys.argv[1:]

    def __exit__(self, type, value, traceback):
        sys.argv = self.argv

with ArgvHider():
    import pygst  # gstreamer
    pygst.require("0.10")
    import gst
    import gobject
    import gtk  # for main loop


# Functions available to stbt scripts
#===========================================================================

def press(*args, **keywords):
    return control.press(*args, **keywords)


def wait_for_match(*args, **keywords):
    if 'directory' not in keywords:
        keywords['directory'] = _caller_dir()
    return display.wait_for_match(*args, **keywords)


def press_until_match(key, image, interval_secs=3, noise_threshold=0.16,
        max_presses=10):
    i = 0
    while True:
        try:
            keywords = {'directory': _caller_dir(),
                        'timeout_secs': interval_secs,
                        'noise_threshold': noise_threshold}
            wait_for_match(image, **keywords)
            return
        except MatchTimeout:
            if i < max_presses:
                press(key)
                i += 1
            else:
                raise


def wait_for_motion(*args, **keywords):
    if 'directory' not in keywords:
        keywords['directory'] = _caller_dir()
    return display.wait_for_motion(*args, **keywords)


# stbt-run initialisation and convenience functions
# (you will need these if writing your own version of stbt-run)
#===========================================================================

def argparser():
    parser = argparse.ArgumentParser(
        prog='stbt run', description='Run an stb-tester test script')
    parser.add_argument('--control',
        help='The remote control to control the stb (default: %(default)s)')
    parser.add_argument('--source-pipeline',
        help='A gstreamer pipeline to use for A/V input (default: '
             '%(default)s)')
    parser.add_argument('--sink-pipeline',
        help='A gstreamer pipeline to use for video output '
             '(default: %(default)s)')

    class IncreaseDebugLevel(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            global _debug_level
            _debug_level += 1
            setattr(namespace, self.dest, _debug_level)

    global _debug_level
    _debug_level = 0
    parser.add_argument('-v', '--verbose', action=IncreaseDebugLevel, nargs=0,
        help='Enable debug output (specify twice to enable GStreamer element '
             'dumps to ./stbt-debug directory)')

    parser.set_defaults(**load_defaults('run'))
    return parser


def init_run(gst_source_pipeline, gst_sink_pipeline, control_uri):
    global display, control
    display = Display(gst_source_pipeline, gst_sink_pipeline)
    control = uri_to_remote(control_uri, display)


def teardown_run():
    display.teardown()


def save_frame(buf, filename):
    '''Save a gstreamer buffer to the specified file in png format.'''
    pipeline = gst.parse_launch(" ! ".join([
                'appsrc name="src" caps="%s"' % buf.get_caps(),
                'ffmpegcolorspace',
                'pngenc',
                'filesink location="%s"' % filename,
                ]))
    src = pipeline.get_by_name("src")
    # This is actually a (synchronous) method call to push-buffer:
    src.emit('push-buffer', buf)
    src.emit('end-of-stream')
    pipeline.set_state(gst.STATE_PLAYING)
    msg = pipeline.get_bus().poll(
        gst.MESSAGE_ERROR | gst.MESSAGE_EOS, 25 * gst.SECOND)
    pipeline.set_state(gst.STATE_NULL)
    if msg.type == gst.MESSAGE_ERROR:
        (e, debug) = msg.parse_error()
        raise RuntimeError(e.message)


class UITestFailure(Exception):
    pass


class MatchTimeout(UITestFailure):
    def __init__(self, screenshot, expected, timeout_secs):
        self.screenshot = screenshot
        self.expected = expected
        self.timeout_secs = timeout_secs


class MotionTimeout(UITestFailure):
    def __init__(self, screenshot, mask, timeout_secs):
        self.screenshot = screenshot
        self.mask = mask
        self.timeout_secs = timeout_secs


class ConfigurationError(Exception):
    pass


# Internal
#===========================================================================

_debug_level = 0


class Display:
    def __init__(self, source_pipeline_description, sink_pipeline_description):
        gobject.threads_init()

        imageprocessing = " ! ".join([
                # Buffer the video stream, dropping frames if downstream
                # processors aren't fast enough:
                "queue name=q leaky=2",
                # Convert to a colorspace that templatematch can handle:
                "ffmpegcolorspace",
                # Detect motion when requested:
                "stbt-motiondetect name=motiondetect enabled=false",
                # OpenCV image-processing library:
                "stbt-templatematch name=templatematch method=1",
                ])
        xvideo = " ! ".join([
                # Convert to a colorspace that xvimagesink can handle:
                "ffmpegcolorspace",
                sink_pipeline_description,
                ])
        screenshot = ("appsink name=screenshot max-buffers=1 drop=true "
                      "sync=false")
        pipe = " ".join([
                imageprocessing,
                "! tee name=t",
                "t. ! queue leaky=2 !", screenshot,
                "t. ! queue leaky=2 !", xvideo
                ])

        # Gstreamer loads plugin libraries on demand, when elements that need
        # those libraries are first mentioned. There is a bug in gst-opencv
        # where it erroneously claims to provide appsink, preventing the
        # loading of the real appsink -- so we load it first.
        # TODO: Fix gst-opencv so that it doesn't prevent appsink from being
        #       loaded.
        gst.parse_launch("appsink")

        self.source_pipeline_description = source_pipeline_description
        self.source_bin = self.create_source_bin()
        self.sink_bin = gst.parse_bin_from_description(pipe, True)

        self.pipeline = gst.Pipeline("stb-tester")
        self.pipeline.add(self.source_bin, self.sink_bin)
        gst.element_link_many(self.source_bin, self.sink_bin)

        self.templatematch = self.pipeline.get_by_name("templatematch")
        self.motiondetect = self.pipeline.get_by_name("motiondetect")
        self.screenshot = self.pipeline.get_by_name("screenshot")
        self.bus = self.pipeline.get_bus()
        self.bus.connect("message::error", self.on_error)
        self.bus.connect("message::warning", self.on_warning)
        self.bus.add_signal_watch()

        if _debug_level > 1:
            if _mkdir("stbt-debug/motiondetect") and _mkdir(
                      "stbt-debug/templatematch"):
                # Note that this will dump a *lot* of files -- several images
                # per frame processed.
                self.motiondetect.props.debugDirectory = (
                    "stbt-debug/motiondetect")
                self.templatematch.props.debugDirectory = (
                    "stbt-debug/templatematch")
            else:
                warn("Failed to create directory 'stbt-debug'. "
                     "Will not enable motiondetect/templatematch debug dump.")

        self.pipeline.set_state(gst.STATE_PLAYING)

        # Handle loss of video (but without end-of-stream event) from the
        # Hauppauge HDPVR capture device.
        self.queue = self.pipeline.get_by_name("q")
        self.successive_underruns = 0
        self.underrun_timeout = None
        self.queue.connect("underrun", self.on_underrun)
        self.queue.connect("running", self.on_running)

    def create_source_bin(self):
        source_bin = gst.parse_bin_from_description(
            self.source_pipeline_description +
                " ! capsfilter name=padforcer caps=video/x-raw-yuv",
            False)
        source_bin.add_pad(
            gst.GhostPad(
                "source",
                source_bin.get_by_name("padforcer").src_pads().next()))
        return source_bin

    def wait_for_match(self, image, directory,
                       timeout_secs=10, consecutive_matches=1,
                       noise_threshold=0.16):
        """Wait for a match of `image` in the source video stream.

        "consecutive_matches" enables to wait for several consecutive frames
        with a match found at the same x,y position.

        "timeout_secs" is in seconds elapsed, as reported by the video stream.

        "noise_threshold" is a threshold used to confirm a potential match.
        """

        if os.path.isabs(image):
            template = image
        else:
            # Image is relative to the script's own directory
            template = os.path.abspath(os.path.join(directory, image))
            if not os.path.isfile(template):
                # Fall back to image from cwd, for convenience of the selftests
                template = os.path.abspath(image)

        self.templatematch.props.template = template
        self.templatematch.props.noiseThreshold = noise_threshold
        self.match_count, self.last_x, self.last_y = 0, 0, 0
        self.consecutive_matches = consecutive_matches

        debug("Searching for " + template)
        self.bus.connect("message::element", self.on_match)
        with GObjectTimeout(timeout_secs, self.on_timeout, self.on_match) as t:
            self.test_timeout = t
            gtk.main()
            if self.match_count == self.consecutive_matches:
                debug("MATCHED " + template)
                return
            else:
                buf = self.screenshot.get_property("last-buffer")
                raise MatchTimeout(buf, template, timeout_secs)

    def wait_for_motion(self, directory,
                        timeout_secs=10, consecutive_frames=10, mask=None):
        """Wait for motion in the source video stream."""

        self.motiondetect.props.enabled = True
        if mask:
            if os.path.isabs(mask):
                mask_path = mask
            else:
                # Mask is relative to the script's own directory
                mask_path = os.path.abspath(os.path.join(directory, mask))
                if not os.path.isfile(mask_path):
                    # Fall back to mask from cwd, for convenience of selftests
                    mask_path = os.path.abspath(mask)
            if not os.path.isfile(mask_path):
                sys.stderr.write("Error: mask '%s' does not exist.\n" % (mask))
                sys.exit(1)
            debug("Using mask %s" % (mask_path))
            self.motiondetect.props.mask = mask_path

        self.consecutive_frames = consecutive_frames
        self.consecutive_frames_count = 0

        debug("Waiting for %d consecutive frames with motion"
                % (consecutive_frames))
        self.bus.connect("message::element", self.on_motion)
        with GObjectTimeout(timeout_secs,
                            self.on_timeout, self.on_motion) as t:
            self.test_timeout = t
            gtk.main()
            if self.consecutive_frames_count == self.consecutive_frames:
                debug("Motion detected.")
                return
            else:
                buf = self.screenshot.get_property("last-buffer")
                raise MotionTimeout(buf, mask, timeout_secs)

    def on_timeout(self, handler):
        debug("Timed out")
        self.bus.disconnect_by_func(handler)
        gtk.main_quit()
        return False  # stop the timeout from running again

    def on_error(self, bus, message):
        assert message.type == gst.MESSAGE_ERROR
        err, dbg = message.parse_error()
        sys.stderr.write("Error: %s: %s\n%s\n" % (err, err.message, dbg))
        sys.exit(1)

    def on_warning(self, bus, message):
        assert message.type == gst.MESSAGE_WARNING
        err, dbg = message.parse_warning()
        sys.stderr.write("Warning: %s: %s\n%s\n" % (err, err.message, dbg))
        if (message.src == self.templatematch and
                err.message == "OpenCV failed to load template image"):
            sys.stderr.write("Error: %s\n" % err.message)
            sys.exit(1)

    def on_match(self, bus, message):
        st = message.structure
        if st.get_name() == "template_match":

            buf = self.screenshot.get_property("last-buffer")
            if not buf:
                return

            matched = st["match"]
            debug("%s %d found at %d,%d (last: %d,%d) with dimensions "
                  "%dx%d. 1st pass: %d%%. Timestamp: %d."
                  % ("Match" if matched else "Weak match",
                     self.match_count,
                     st["x"], st["y"], self.last_x, self.last_y,
                     st["width"], st["height"], 100 * st["first_pass_result"],
                     buf.timestamp))

            if matched and (
                    self.match_count == 0 or
                    (st["x"], st["y"]) == (self.last_x, self.last_y)):
                self.match_count += 1
            else:
                self.match_count = 0
            self.last_x, self.last_y = st["x"], st["y"]

            if self.match_count == self.consecutive_matches:
                self.templatematch.props.template = None
                self.bus.disconnect_by_func(self.on_match)
                gtk.main_quit()

    def on_motion(self, bus, message):
        st = message.structure
        if st.get_name() == "motiondetect":
            buf = self.screenshot.get_property("last-buffer")
            if not buf:
                return

            has_motion = st["has_motion"]
            if has_motion:
                self.consecutive_frames_count += 1
                debug("Motion detected. Timestamp: %d." % (buf.timestamp))
            else:
                self.consecutive_frames_count = 0
                debug("No Motion detected. Timestamp: %d." % (buf.timestamp))

            if self.consecutive_frames_count == self.consecutive_frames:
                self.motiondetect.props.mask = None
                self.motiondetect.enabled = False
                self.bus.disconnect_by_func(self.on_motion)
                gtk.main_quit()

    def on_underrun(self, element):
        if self.underrun_timeout:
            ddebug("underrun: I already saw a recent underrun; ignoring")
        else:
            ddebug("underrun: scheduling 'restart_source_bin' in 2s")
            self.underrun_timeout = GObjectTimeout(2, self.restart_source_bin)
            self.underrun_timeout.start()

    def on_running(self, element):
        if self.underrun_timeout:
            ddebug("running: cancelling underrun timer")
            self.successive_underruns = 0
            self.underrun_timeout.cancel()
            self.underrun_timeout = None
        else:
            ddebug("running: no outstanding underrun timers; ignoring")

    def restart_source_bin(self):

        self.successive_underruns += 1
        if self.successive_underruns <= 3:
            self.test_timeout.cancel()
            gst.element_unlink_many(self.source_bin, self.sink_bin)
            self.source_bin.set_state(gst.STATE_NULL)
            self.sink_bin.set_state(gst.STATE_READY)
            self.pipeline.remove(self.source_bin)
            self.source_bin = None
            debug("Attempting to recover from video loss: "
                  "Stopping source pipeline and waiting 5s...")
            time.sleep(5)

            debug("Restarting source pipeline...")
            self.source_bin = self.create_source_bin()
            self.pipeline.add(self.source_bin)
            gst.element_link_many(self.source_bin, self.sink_bin)
            self.source_bin.set_state(gst.STATE_PLAYING)
            self.pipeline.set_state(gst.STATE_PLAYING)
            debug("Restarted source pipeline")

            self.underrun_timeout.start()

            self.test_timeout.start()
        return False  # stop the timeout from running again

    def teardown(self):
        if self.pipeline:
            self.pipeline.send_event(gst.event_new_eos())
            self.pipeline.set_state(gst.STATE_NULL)


class GObjectTimeout:
    """Responsible for setting a timeout in the GTK main loop.

    Can be used as a Context Manager in a 'with' statement.
    """
    def __init__(self, timeout_secs, handler, *args):
        self.timeout_secs = timeout_secs
        self.handler = handler
        self.args = args
        self.timeout_id = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, type, value, traceback):
        self.cancel()

    def start(self):
        self.timeout_id = gobject.timeout_add(
            self.timeout_secs * 1000, self.handler, *self.args)

    def cancel(self):
        if self.timeout_id:
            gobject.source_remove(self.timeout_id)
        self.timeout_id = None


def uri_to_remote(uri, display):
    if uri.lower() == 'none':
        return NullRemote()
    if uri.lower() == 'test':
        return VideoTestSrcControl(display)
    vr = re.match(r'vr:(?P<hostname>[^:]*)(:(?P<port>\d+))?', uri)
    if vr:
        d = vr.groupdict()
        return VirtualRemote(d['hostname'], int(d['port'] or 2033))
    lirc = re.match(r'lirc:(?P<lircd_socket>[^:]*):(?P<control_name>.*)', uri)
    if lirc:
        d = lirc.groupdict()
        return LircRemote(d['lircd_socket'] or '/var/run/lirc/lircd',
                          d['control_name'])
    raise ConfigurationError('Invalid remote control URI: "%s"' % uri)


class NullRemote:
    def press(self, key):
        debug('NullRemote: Ignoring request to press "%s"' % key)


class VideoTestSrcControl:
    """Remote control used by selftests.

    Changes the videotestsrc image to the specified pattern ("0" to "20").
    See `gst-inspect videotestsrc`.
    """
    def __init__(self, display):
        self.videosrc = display.pipeline.get_by_name("videotestsrc0")
        if not self.videosrc:
            raise ConfigurationError('The "test" control can only be used'
                                     'with source-pipeline = "videotestsrc"')

    def press(self, key):
        if key not in [str(x) for x in range(21)]:
            raise UITestFailure('Key "%s" not valid for the "test" control'
                                ' (only "0" to "20" allowed)' % key)
        self.videosrc.props.pattern = int(key)
        debug("Pressed " + key)


class VirtualRemote:
    """Send a key-press to a set-top box running a VirtualRemote listener.

        control = VirtualRemote("192.168.0.123")
        control.press("MENU")
    """
    def __init__(self, hostname, port):
        self.s = socket.socket()
        debug("VirtualRemote: Connecting to %s:%d" % (hostname, port))
        try:
            self.s.settimeout(3)
            self.s.connect((hostname, port))
            self.s.settimeout(None)
            debug("VirtualRemote: Connected to %s:%d" % (hostname, port))
        except socket.error as e:
            e.args = (("Failed to connect to VirtualRemote at %s:%d: %s" % (
                hostname, port, e)),)
            e.strerror = e.args[0]
            raise

    def press(self, key):
        self.s.send("D\t%s\n\0U\t%s\n\0" % (key, key))  # key Down, then key Up
        debug("Pressed " + key)

    def close(self):
        self.s.close()
        self.s = None


class LircRemote:
    """Send a key-press via a LIRC-enabled infrared blaster.

    See http://www.lirc.org/html/technical.html#applications
    """
    def __init__(self, lircd_socket, control_name):
        self.control_name = control_name
        self.lircd = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        debug("LircRemote: Connecting to %s" % lircd_socket)
        try:
            self.lircd.settimeout(3)
            self.lircd.connect(lircd_socket)
            self.lircd.settimeout(None)
            debug("LircRemote: Connected to %s" % lircd_socket)
        except socket.error as e:
            e.args = (("Failed to connect to Lirc socket %s: %s" % (
                        lircd_socket, e)),)
            e.strerror = e.args[0]
            raise

    def press(self, key):
        self.lircd.send("SEND_ONCE %s %s\n" % (self.control_name, key))
        debug("Pressed " + key)

    def close(self):
        self.lircd.close()
        self.lircd = None


def uri_to_remote_recorder(uri):
    vr = re.match(r'vr:(?P<hostname>[^:]*)(:(?P<port>\d+))?', uri)
    if vr:
        d = vr.groupdict()
        return virtual_remote_listen(d['hostname'], int(d['port'] or 2033))
    lirc = re.match(r'lirc:(?P<lircd_socket>[^:]*):(?P<control_name>.*)', uri)
    if lirc:
        d = lirc.groupdict()
        return lirc_remote_listen(d['lircd_socket'] or '/var/run/lirc/lircd',
                                  d['control_name'])
    f = re.match('file://(?P<filename>.+)', uri)
    if f:
        return file_remote_recorder(f.group('filename'))
    raise ConfigurationError('Invalid remote control recorder URI: "%s"' % uri)


def file_remote_recorder(filename):
    """ A generator that returns lines from the file given by filename.

    Unfortunately treating a file as a iterator doesn't work in the case of
    interactive input, even when we provide bufsize=1 (line buffered) to the
    call to open() so we have to have this function to work around it. """
    f = open(filename, 'r')
    if filename == '/dev/stdin':
        sys.stderr.write('Waiting for keypresses from standard input...\n')
    while True:
        line = f.readline()
        if line == '':
            f.close()
            raise StopIteration
        yield line.rstrip()


def read_records(stream, sep):
    r"""Generator that splits stream into records given a separator

    >>> import StringIO
    >>> s = StringIO.StringIO('hello\n\0This\n\0is\n\0a\n\0test\n\0')
    >>> list(read_records(FileToSocket(s), '\n\0'))
    ['hello', 'This', 'is', 'a', 'test']
    """
    buf = ""
    l = len(sep)
    while True:
        s = stream.recv(4096)
        if len(s) == 0:
            break
        buf += s
        cmds = buf.split(sep)
        buf = cmds[-1]
        for i in cmds[:-1]:
            yield i


def vr_key_reader(cmd_iter):
    r"""Converts virtual remote records into list of keypresses

    >>> list(vr_key_reader(['D\tHELLO', 'U\tHELLO']))
    ['HELLO']
    >>> list(vr_key_reader(['D\tCHEESE', 'D\tHELLO', 'U\tHELLO', 'U\tCHEESE']))
    ['HELLO', 'CHEESE']
    """
    for i in cmd_iter:
        (action, key) = i.split('\t')
        if action == 'U':
            yield key


def virtual_remote_listen(address, port):
    """Waits for a VirtualRemote to connect, and returns an iterator yielding
    keypresses."""
    serversocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    serversocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    serversocket.bind((address, port))
    serversocket.listen(5)
    sys.stderr.write("Waiting for connection from virtual remote control "
                     "on %s:%d...\n" % (address, port))
    (connection, address) = serversocket.accept()
    sys.stderr.write("Accepted connection from %s\n" % str(address))
    return vr_key_reader(read_records(connection, '\n\0'))


def lirc_remote_listen(lircd_socket, control_name):
    """Returns an iterator yielding keypresses received from lircd.

    See http://www.lirc.org/html/technical.html#applications
    """
    lircd = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    debug("control-recorder connecting to lirc socket '%s'..." % lircd_socket)
    lircd.connect(lircd_socket)
    debug("control-recorder connected to lirc socket")
    return lirc_key_reader(lircd.makefile(), control_name)


def lirc_key_reader(cmd_iter, control_name):
    r"""Convert lircd messages into list of keypresses

    >>> list(lirc_key_reader(['0000dead 00 MENU My-IR-remote',
    ...                       '0000beef 00 OK My-IR-remote',
    ...                       '0000f00b 01 OK My-IR-remote',
    ...                       'BEGIN', 'SIGHUP', 'END'],
    ...                      'My-IR-remote'))
    ['MENU', 'OK']
    """
    for s in cmd_iter:
        debug("lirc_key_reader received: %s" % s.rstrip())
        m = re.match(r"\w+ (?P<repeat_count>\d+) (?P<key>\w+) %s" %
                         control_name,
                     s)
        if m and int(m.group('repeat_count')) == 0:
            yield m.group('key')


def load_defaults(tool):
    conffile = ConfigParser.SafeConfigParser()
    conffile.add_section('global')
    conffile.add_section(tool)

    # When run from the installed location (as `stbt run`), will read config
    # from $SYSCONFDIR/stbt/stbt.conf (see `stbt.in`); when run from the source
    # directory (as `stbt-run`) will read config from the source directory.
    system_config = os.environ.get(
        'STBT_SYSTEM_CONFIG',
        os.path.join(os.path.dirname(__file__), 'stbt.conf'))

    files_read = conffile.read([
        system_config,
        # User config: ~/.config/stbt/stbt.conf, as per freedesktop's base
        # directory specification:
        '%s/stbt/stbt.conf' % os.environ.get('XDG_CONFIG_HOME',
                                            '%s/.config' % os.environ['HOME']),
        # Config files specific to the test suite / test run:
        os.environ.get('STBT_CONFIG_FILE', ''),
        ])
    assert(system_config in files_read)
    return dict(conffile.items('global'), **dict(conffile.items(tool)))


def _caller_dir():
    # stack()[0] is _caller_dir;
    # stack()[1] is _caller_dir's caller "f";
    # stack()[2] is f's caller.
    import inspect
    return os.path.dirname(
        inspect.getframeinfo(inspect.stack()[2][0]).filename)


def _mkdir(d):
    try:
        os.makedirs(d)
    except OSError, e:
        if e.errno != errno.EEXIST:
            return False
    return os.path.isdir(d) and os.access(d, os.R_OK | os.W_OK)


def debug(s):
    if _debug_level > 0:
        sys.stderr.write("%s: %s\n" % (os.path.basename(sys.argv[0]), str(s)))


def ddebug(s):
    """Extra verbose debug for stbt developers, not end users"""
    if _debug_level > 1:
        sys.stderr.write("%s: %s\n" % (os.path.basename(sys.argv[0]), str(s)))


def warn(s):
    sys.stderr.write("%s: warning: %s\n" % (
            os.path.basename(sys.argv[0]), str(s)))


# Tests
#===========================================================================

class FileToSocket:
    """Makes something File-like behave like a Socket for testing purposes

    >>> import StringIO
    >>> s = FileToSocket(StringIO.StringIO("Hello"))
    >>> s.recv(3)
    'Hel'
    >>> s.recv(3)
    'lo'
    """
    def __init__(self, f):
        self.file = f

    def recv(self, bufsize, flags=0):
        return self.file.read(bufsize)


def test_that_virtual_remote_is_symmetric_with_virtual_remote_listen():
    import threading
    import time
    t = threading.Thread()
    vrl = []
    t.run = lambda: vrl.append(virtual_remote_listen('localhost', 2033))
    t.start()
    time.sleep(0.01)  # Give it a chance to start listening (sorry)
    vr = VirtualRemote('localhost', 2033)
    t.join()
    for i in ['DOWN', 'DOWN', 'UP', 'GOODBYE']:
        vr.press(i)
    vr.close()
    assert list(vrl[0]) == ['DOWN', 'DOWN', 'UP', 'GOODBYE']


def fake_lircd(address):
    import tempfile

    # This needs to accept 2 connections (from LircRemote and
    # lirc_remote_listen) and, on receiving input from the LircRemote
    # connection, write to the lirc_remote_listen connection.
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(address)
    s.listen(5)
    listener, _ = s.accept()
    control, _ = s.accept()
    for cmd in control.makefile():
        m = re.match(r'SEND_ONCE (?P<control>\w+) (?P<key>\w+)', cmd)
        if m:
            d = m.groupdict()
            listener.send('00000000 0 %s %s\n' % (d['key'], d['control']))


def test_that_lirc_remote_is_symmetric_with_lirc_remote_listen():
    import tempfile
    import threading
    import time

    lircd_socket = tempfile.mktemp()
    t = threading.Thread()
    t.run = lambda: fake_lircd(lircd_socket)
    t.start()
    time.sleep(0.01)  # Give it a chance to start listening (sorry)
    listener = lirc_remote_listen(lircd_socket, 'test')
    control = LircRemote(lircd_socket, 'test')
    for i in ['DOWN', 'DOWN', 'UP', 'GOODBYE']:
        control.press(i)
        assert listener.next() == i
    control.close()
    t.join()
