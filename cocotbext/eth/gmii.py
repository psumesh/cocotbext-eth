"""

Copyright (c) 2020 Alex Forencich

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.

"""

import cocotb
from cocotb.triggers import RisingEdge, ReadOnly, Timer, First, Event
from cocotb.bus import Bus
from cocotb.log import SimLog
from cocotb.utils import get_sim_time

from collections import deque

from .constants import EthPre, ETH_PREAMBLE

class GmiiFrame(object):
    def __init__(self, data=None, error=None):
        self.data = bytearray()
        self.error = None
        self.rx_sim_time = None

        if type(data) is GmiiFrame:
            self.data = bytearray(data.data)
            self.error = data.error
            self.rx_sim_time = data.rx_sim_time
        else:
            self.data = bytearray(data)
            self.error = error

    @classmethod
    def from_payload(cls, payload):
        data = bytearray(ETH_PREAMBLE)
        data.extend(payload)
        return cls(data)

    def get_preamble(self):
        return self.data[0:8]

    def get_payload(self):
        return self.data[8:]

    def normalize(self):
        n = len(self.data)

        if self.error is not None:
            self.error = self.error[:n] + [self.error[-1]]*(n-len(self.error))
        else:
            self.error = [0]*n

    def compact(self):
        if not any(self.error):
            self.error = None

    def __eq__(self, other):
        if type(other) is GmiiFrame:
            return self.data == other.data

    def __repr__(self):
        return (
                f"{type(self).__name__}(data={repr(self.data)}, " +
                f"error={repr(self.error)}, " +
                f"rx_sim_time={repr(self.rx_sim_time)})"
            )

    def __len__(self):
        return len(self.data)

    def __iter__(self):
        return self.data.__iter__()


class GmiiSource(object):

    _signals = ["d"]
    _optional_signals = ["er", "en", "dv"]

    def __init__(self, entity, name, clock, reset=None, enable=None, *args, **kwargs):
        self.log = SimLog("cocotb.%s.%s" % (entity._name, name))
        self.entity = entity
        self.clock = clock
        self.reset = reset
        self.enable = enable
        self.bus = Bus(self.entity, name, self._signals, optional_signals=self._optional_signals, **kwargs)

        super().__init__(*args, **kwargs)

        self.active = False
        self.queue = deque()

        self.ifg = 12

        self.queue_occupancy_bytes = 0
        self.queue_occupancy_frames = 0

        self.width = 8
        self.byte_width = 1

        self.reset = reset

        assert len(self.bus.d) == 8
        self.bus.d.setimmediatevalue(0)
        if self.bus.er is not None:
            assert len(self.bus.er) == 1
            self.bus.er.setimmediatevalue(0)
        if self.bus.en is not None:
            assert len(self.bus.en) == 1
            self.bus.en.setimmediatevalue(0)
            self.bus.dv = self.bus.en
        if self.bus.dv is not None:
            assert len(self.bus.dv) == 1
            self.bus.dv.setimmediatevalue(0)

        cocotb.fork(self._run())

    def send(self, frame):
        frame = GmiiFrame(frame)
        self.queue_occupancy_bytes += len(frame)
        self.queue_occupancy_frames += 1
        self.queue.append(frame)

    def count(self):
        return len(self.queue)

    def empty(self):
        return not self.queue

    def idle(self):
        return self.empty() and not self.active

    async def wait(self):
        while not self.idle():
            await RisingEdge(self.clock)

    async def _run(self):
        frame = None
        ifg_cnt = 0
        self.active = False

        while True:
            await ReadOnly()

            if self.reset is not None and self.reset.value:
                await RisingEdge(self.clock)
                frame = None
                ifg_cnt = 0
                self.active = False
                self.bus.d <= 0
                if self.bus.er is not None:
                    self.bus.er <= 0
                self.bus.en <= 0
                continue

            await RisingEdge(self.clock)

            if self.enable is None or self.enable.value:
                if ifg_cnt > 0:
                    # in IFG
                    ifg_cnt -= 1

                elif frame is None and self.queue:
                    # send frame
                    frame = self.queue.popleft()
                    self.queue_occupancy_bytes -= len(frame)
                    self.queue_occupancy_frames -= 1
                    self.log.info(f"TX frame: {frame}")
                    frame.normalize()
                    self.active = True

                if frame is not None:
                    self.bus.d <= frame.data.pop(0)
                    if self.bus.er is not None:
                        self.bus.er <= frame.error.pop(0)
                    self.bus.en <= 1

                    if not frame.data:
                        ifg_cnt = max(self.ifg, 1)
                        frame = None
                else:
                    self.bus.d <= 0
                    if self.bus.er is not None:
                        self.bus.er <= 0
                    self.bus.en <= 0
                    self.active = False


class GmiiSink(object):

    _signals = ["d"]
    _optional_signals = ["er", "en", "dv"]

    def __init__(self, entity, name, clock, reset=None, enable=None, *args, **kwargs):
        self.log = SimLog("cocotb.%s.%s" % (entity._name, name))
        self.entity = entity
        self.clock = clock
        self.reset = reset
        self.enable = enable
        self.bus = Bus(self.entity, name, self._signals, optional_signals=self._optional_signals, **kwargs)

        super().__init__(*args, **kwargs)

        self.active = False
        self.queue = deque()
        self.sync = Event()

        self.queue_occupancy_bytes = 0
        self.queue_occupancy_frames = 0

        self.width = 8
        self.byte_width = 1

        self.reset = reset

        assert len(self.bus.d) == 8
        if self.bus.er is not None:
            assert len(self.bus.er) == 1
        if self.bus.en is not None:
            assert len(self.bus.en) == 1
            self.bus.dv = self.bus.en
        if self.bus.dv is not None:
            assert len(self.bus.dv) == 1

        cocotb.fork(self._run())

    def recv(self):
        if self.queue:
            frame = self.queue.popleft()
            self.queue_occupancy_bytes -= len(frame)
            self.queue_occupancy_frames -= 1
            return frame
        return None

    def count(self):
        return len(self.queue)

    def empty(self):
        return not self.queue

    def idle(self):
        return not self.active

    async def wait(self, timeout=0, timeout_unit=None):
        if not self.empty():
            return
        self.sync.clear()
        if timeout:
            await First(self.sync.wait(), Timer(timeout, timeout_unit))
        else:
            await self.sync.wait()

    async def _run(self):
        frame = None
        self.active = False

        while True:
            await ReadOnly()

            if self.reset is not None and self.reset.value:
                await RisingEdge(self.clock)
                frame = None
                self.active = False
                continue

            if self.enable is None or self.enable.value:
                d_val = self.bus.d.value.integer
                dv_val = self.bus.dv.value
                er_val = 0 if self.bus.er is None else self.bus.er.value

                if frame is None:
                    if dv_val:
                        # start of frame
                        frame = GmiiFrame(bytearray(), [])
                        frame.rx_sim_time = get_sim_time()
                else:
                    if not dv_val:
                        # end of frame

                        frame.compact()
                        self.log.info(f"RX frame: {frame}")

                        self.queue_occupancy_bytes += len(frame)
                        self.queue_occupancy_frames += 1

                        self.queue.append(frame)
                        self.sync.set()

                        frame = None

                if frame is not None:
                    frame.data.append(d_val)
                    frame.error.append(er_val)

            await RisingEdge(self.clock)


class GmiiMonitor(GmiiSink):
    pass
