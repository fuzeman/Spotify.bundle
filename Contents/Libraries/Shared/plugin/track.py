from plugin.range import Range
from plugin.stream import Stream

from pyemitter import Emitter
from threading import Event, Timer, Lock
import logging
import time

log = logging.getLogger(__name__)


class Track(Emitter):
    reuse_distance = 1024 * 1024  # 1MB (in bytes)
    final_distance = 128 * 1024   # 128kB (in bytes)
    limit_seconds = 10

    def __init__(self, server, uri):
        self.server = server
        self.uri = uri

        self.metadata = None
        self.metadata_ev = Event()

        self.info = None
        self.info_ev = Event()

        self.setup_lock = Lock()

        self.buffer = bytearray()

        self.streams = {}
        self.stream_lock = Lock()

        self.limit_timer = None

        # Track state
        self.reading_start = None

        self.playing = False
        self.ended = False

    def setup(self):
        with self.setup_lock:
            if self.metadata is None:
                # Fetch metadata
                self.server.sp.metadata(self.uri, self.on_metadata)
                self.metadata_ev.wait()

            if self.info is None:
                # Fetch stream info
                self.metadata.track_uri(self.on_track_uri) \
                    .on('error', self.on_track_error)

                self.info_ev.wait(timeout=5)

    def on_metadata(self, metadata):
        self.metadata = metadata

        uri = self.metadata.uri

        # Ensure track is available, find alternative
        if not self.metadata.is_available():
            log.info('[%s] Track is not available, looking for an alternative...', uri)

            # Try find alternative track that is available
            if self.metadata.find_alternative():
                log.info('[%s] Alternative found (uri: "%s")', uri, self.metadata.uri)
            else:
                log.warn('[%s] No alternatives could be found', uri)

        # Log track restrictions for debugging
        for x, restriction in enumerate(self.metadata.restrictions):
            log.info(
                '[%s] R#%s countries allowed: %s, countries forbidden: %s, catalogues: %s', uri, x + 1,
                restriction.countries_allowed, restriction.countries_forbidden, restriction.catalogues
            )

        self.metadata_ev.set()
        self.emit('metadata', self.metadata)

    def on_track_uri(self, response):
        self.info = response.get('result')
        log.debug('received track info: %s', self.info)

        self.info_ev.set()
        self.emit('track_uri', self.info)

        # Track has started
        self.on_start()

    def on_track_error(self, error):
        self.info = None
        log.warn('track error: %s', error)

        self.info_ev.set()
        self.emit('track_uri', self.info)

    def stream(self, r_range):
        """
        :type r_range: plugin.range.Range
        :rtype: plugin.stream.Stream
        """

        with self.stream_lock:
            if r_range is None:
                r_range = Range(0, None)

            # Check for existing stream (with same range)
            if r_range.tuple() in self.streams:
                log.info('Returning existing stream (r_range: %s)', repr(r_range))

                # Ensure we are setup to stream (metadata, info)
                self.setup()

                return self.streams[r_range.tuple()]

            for s_range in self.streams:
                stream = self.streams[s_range]

                s_start, s_end = s_range

                # Ensure stream contains the range-start
                if s_start > r_range.start:
                    continue

                # Ensure stream contains the range-end
                if s_end != r_range.end:
                    if r_range.end is None or s_end is None:
                        continue

                    if s_end < r_range.end:
                        continue

                # Wait until the stream has opened
                if not stream.on_open.wait(5):
                    log.info('Timeout while waiting for stream to open')
                    continue

                # Check if we should open a new stream
                buf_distance = (r_range.start - s_start) - len(stream.buffer)
                end_distance = stream.total_length - r_range.start

                if buf_distance > self.reuse_distance and end_distance < self.final_distance:
                    # TODO - Ensure stream-source isn't about to expire
                    log.info(
                        "Buffer is %s bytes away and range is %s bytes from the end of the track - ignoring it",
                        buf_distance, end_distance
                    )
                    continue

                log.info('Returning existing stream with similar range (s_range: %s)', repr(s_range))

                # Ensure we are setup to stream (metadata, info)
                self.setup()

                return self.streams[s_range]

            log.info('Building stream for track (r_range: %s)', repr(r_range))

            # Create new stream
            stream = Stream(self, len(self.streams), r_range)

            # Store stream
            self.streams[r_range.tuple()] = stream

        # Ensure we are setup to stream (metadata, info)
        self.setup()

        # Validate stream info
        if not self.info or 'uri' not in self.info:
            return None

        self.limit_set()
        return stream

    def limit_set(self):
        # Ensure we have at least two streams
        if len(self.streams) < 2:
            return

        # Find active streams
        streams_active = [
            (r_range, stream)
            for r_range, stream in self.streams.items()
            if stream.state == 'reading'
        ]

        # Ignore if this is the first active stream
        if len(streams_active) < 1:
            return

        log.debug('Multiple active streams, setting up rate-limits')

        for (start, end), stream in self.streams.items():
            if not start and not end:
                log.info('Stream rate-limiting enabled on %s', stream)
                stream.on_reading.clear()
                continue

            log.info('Stream priority enabled on %s', stream)

            stream.once('buffered', self.limit_buffered)

    def limit_buffered(self):
        ready = all([
            stream.state == 'buffered'
            for stream in self.streams.values()
            if stream.on_reading.is_set()
        ])

        if not ready:
            return

        log.info('Priority streams have been buffered')
        self.limit_reset()

    def limit_reset(self):
        if self.limit_timer:
            self.limit_timer.cancel()
            self.limit_timer = None

        limited = False

        for range, stream in self.streams.items():
            limited |= not stream.on_reading.is_set()

            stream.on_reading.set()

        if limited:
            log.info('Stream rate-limiting disabled')

    def on_start(self):
        if self.playing:
            return

        log.info('[%s] Started', self.uri)

        # Schedule limit reset
        self.limit_timer = Timer(self.limit_seconds, self.limit_reset)
        self.limit_timer.start()

        # Fire track start event
        self.metadata.track_event(self.info['lid'], 3, 0)

        self.reading_start = time.time()
        self.playing = True

    @property
    def position(self):
        """Get estimated player position for track

        :returns: Position (in milliseconds)
        :rtype: int
        """
        position = 0

        if self.reading_start:
            position = int((time.time() - self.reading_start) * 1000)

        if position > self.metadata.duration:
            return self.metadata.duration

        return position

    def end(self):
        """Send track end/completion events"""
        if not self.playing or self.ended:
            log.warn('[%s] Invalid Track.end() call (playing: %s, ended: %s)', self.uri, self.playing, self.ended)
            return

        log.debug('[%s] Sending "track_end" event (position: %s)', self.uri, self.position)

        self.metadata.track_end(self.info['lid'], self.position)
        self.ended = True
