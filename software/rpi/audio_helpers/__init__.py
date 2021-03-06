# Copyright (C) 2017 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Helper functions for audio streams."""

import logging
import threading
import time
import wave

import sounddevice as sd


class WaveSource(object):
    """Audio source that reads audio data from a WAV file.

    Reads are throttled to emulate the given sample rate and silence
    is returned when the end of the file is reached.

    Args:
      fp: file-like stream object to read from.
      sample_rate: sample rate in hertz.
      sample_width: size of a single sample in bytes.

    """
    def __init__(self, fp, sample_rate, sample_width):
        self._fp = fp
        try:
            self._wavep = wave.open(self._fp, 'r')
        except wave.Error as e:
            logging.warning('error opening WAV file: %s, '
                            'falling back to RAW format', e)
            self._fp.seek(0)
            self._wavep = None
        self._sample_rate = sample_rate
        self._sample_width = sample_width
        self._sleep_until = 0

    def read(self, size):
        """Read bytes from the stream and block until sample rate is achieved.

        Args:
          size: number of bytes to read from the stream.
        """
        now = time.time()
        missing_dt = self._sleep_until - now
        if missing_dt > 0:
            time.sleep(missing_dt)
        self._sleep_until = time.time() + self._sleep_time(size)
        data = (self._wavep.readframes(size)
                if self._wavep
                else self._fp.read(size))
        #  When reach end of audio stream, pad remainder with silence (zeros).
        if not data:
            return b'\x00' * size
        return data

    def close(self):
        """Close the underlying stream."""
        if self._wavep:
            self._wavep.close()
        self._fp.close()

    def _sleep_time(self, size):
        sample_count = size / float(self._sample_width)
        sample_rate_dt = sample_count / float(self._sample_rate)
        return sample_rate_dt

    def start(self):
        pass

    def stop(self):
        pass


class WaveSink(object):
    """Audio sink that writes audio data to a WAV file.

    Args:
      fp: file-like stream object to write data to.
      sample_rate: sample rate in hertz.
      sample_width: size of a single sample in bytes.
    """
    def __init__(self, fp, sample_rate, sample_width):
        self._fp = fp
        self._wavep = wave.open(self._fp, 'wb')
        self._wavep.setsampwidth(sample_width)
        self._wavep.setnchannels(1)
        self._wavep.setframerate(sample_rate)

    def write(self, data):
        """Write bytes to the stream.

        Args:
          data: frame data to write.
        """
        self._wavep.writeframes(data)

    def close(self):
        """Close the underlying stream."""
        self._wavep.close()
        self._fp.close()

    def start(self):
        pass

    def stop(self):
        pass


class SoundDeviceStream(object):
    """Audio stream based on an underlying sound device.

    It can be used as an audio source (read) and a audio source
    (write).

    Args:
      sample_rate: sample rate in hertz.
      sample_width: size of a single sample in bytes.
      block_size: size in bytes of each read and write operation.
      flush_size: size in bytes of silence data written during flush operation.
    """
    def __init__(self, sample_rate, sample_width, block_size, flush_size):
        if sample_width == 2:
            audio_format = 'int16'
        else:
            raise Exception('unsupported sample size:', sample_width)
        self._audio_stream = sd.RawStream(
            samplerate=sample_rate, dtype=audio_format, channels=1,
            blocksize=int(block_size/2),  # blocksize is in number of frames.
        )
        self._block_size = block_size
        self._flush_size = flush_size

    def read(self, size):
        """Read bytes from the stream."""
        buf, overflow = self._audio_stream.read(size)
        if overflow:
            logging.warning('SoundDeviceStream read overflow (%d, %d)',
                            size, len(buf))
        return bytes(buf)

    def write(self, buf):
        """Write bytes to the stream."""
        underflow = self._audio_stream.write(buf)
        if underflow:
            logging.warning('SoundDeviceStream write underflow (size: %d)',
                            len(buf))
        return len(buf)

    def flush(self):
        if self._flush_size > 0:
            self._audio_stream.write(b'\x00' * self._flush_size)

    def start(self):
        """Start the underlying stream."""
        if not self._audio_stream.active:
            self._audio_stream.start()

    def stop(self):
        """Stop the underlying stream."""
        if self._audio_stream.active:
            self.flush()
            self._audio_stream.stop()

    def close(self):
        """Close the underlying stream and audio interface."""
        if self._audio_stream:
            self.stop()
            self._audio_stream.close()
            self._audio_stream = None


class ConversationStream(object):
    """Audio stream that supports half-duplex conversation.

    A conversation is the alternance of:
    - a recording operation
    - a playback operation

    Excepted usage:

      For each conversation:
      - start_recording()
      - read() or iter()
      - stop_recording()
      - start_playback()
      - write()
      - stop_playback()

      When conversations are finished:
      - close()

    Args:
      source: file-like stream object to read input audio bytes from.
      sink: file-like stream object to write output audio bytes to.
      iter_size: read size in bytes for each iteration.
    """
    def __init__(self, source, sink, iter_size):
        self._source = source
        self._sink = sink
        self._iter_size = iter_size
        self._stop_recording = threading.Event()
        self._start_playback = threading.Event()

    def start_recording(self):
        """Start recording from the audio source."""
        self._stop_recording.clear()
        self._source.start()
        self._sink.start()

    def stop_recording(self):
        """Stop recording from the audio source."""
        self._stop_recording.set()

    def start_playback(self):
        """Start playback to the audio sink."""
        self._start_playback.set()

    def stop_playback(self):
        """Stop playback from the audio sink."""
        self._start_playback.clear()
        self._source.stop()
        self._sink.stop()

    def read(self, size):
        """Read bytes from the source (if currently recording).

        Will returns an empty byte string, if stop_recording() was called.
        """
        if self._stop_recording.is_set():
            return b''
        return self._source.read(size)

    def write(self, buf):
        """Write bytes to the sink (if currently playing).

        Will block until start_playback() is called.
        """
        self._start_playback.wait()
        return self._sink.write(buf)

    def close(self):
        """Close source and sink."""
        self._source.close()
        self._sink.close()

    def __iter__(self):
        """Returns a generator reading data from the stream."""
        return iter(lambda: self.read(self._iter_size), b'')
