import argparse
import asyncio
import json
import logging
import serial
import time
import socketio
import requests

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Constants
DEFAULT_SERIAL_PORT = "/dev/ttyACM0"
DEFAULT_BAUDRATE = 115200
DEFAULT_CNCJS_URL = "http://localhost"
DEFAULT_CNCJS_PORT = 8000
PENDANT_REPORT_INTERVAL = 0.02  # 20ms minimum
JOG_FEED_RATE = 1000  # F1000
SERIAL_READ_TIMEOUT = 0.1  # 100ms
JOG_IDLE_FLUSH_TIMEOUT = 0.1  # flush jog queue after 100ms without pendant reports
COMMAND_WRITE_INTERVAL = 0.1  # Minimum 100ms between command writes
GRBL_JOG_CANCEL = '\x85'  # GRBL extended-ASCII realtime jog cancel

class PendantBridge:
    def __init__(self, serial_port, baudrate, cncjs_url, cncjs_port, jog_idle_flush_timeout):
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.cncjs_url = cncjs_url
        self.cncjs_port = cncjs_port
        self.jog_idle_flush_timeout = jog_idle_flush_timeout
        self.serial_connection = None
        self.sio = socketio.AsyncClient()
        self.grbl_port = None
        self.running = True
        self.last_pendant_message_time = None
        self.jog_flush_pending = False
        self._jog_pending = None
        self._jog_notify = asyncio.Event()
        self._last_write_time = 0  # Track last command write time for 100ms interval

    async def connect_serial(self):
        """Connect to pendant microcontroller via serial port."""
        try:
            self.serial_connection = serial.Serial(
                port=self.serial_port,
                baudrate=self.baudrate,
                timeout=SERIAL_READ_TIMEOUT
            )
            logger.info(f"Connected to pendant on {self.serial_port} at {self.baudrate} baud")
            return True
        except serial.SerialException as e:
            logger.error(f"Failed to connect to serial port {self.serial_port}: {e}")
            return False

    async def connect_cncjs(self):
        """Connect to CNCjs server via Socket.IO."""
        # Remove trailing slashes and construct proper URL
        base_url = self.cncjs_url.rstrip('/')
        url = f"{base_url}:{self.cncjs_port}"
        max_retries = 30
        retry_count = 0

        while retry_count < max_retries:
            try:
                # CNCjs login
                res = requests.post(
                    "http://localhost:8000/api/signin",
                    json={
                        "token": "",
                    }
                )

                token = res.json()["token"]
                logger.info(f"Obtained CNCjs access token {token}")

                await self.sio.connect(url, headers={"Authorization": f"Bearer {token}"})
                logger.info(f"Connected to CNCjs server at {url}")
                return True
            except Exception as e:
                retry_count += 1
                logger.warning(f"Failed to connect to CNCjs (attempt {retry_count}/{max_retries}): {e}")
                await asyncio.sleep(1)

        logger.error(f"Failed to connect to CNCjs after {max_retries} attempts")
        return False

    async def wait_for_grbl_connection(self):
        """Wait for GRBL board to be ready on CNCjs."""
        event_received = asyncio.Event()

        @self.sio.on('serialport:change')
        def on_serialport_change(data):
            if data.get('port') and data.get('inuse', False):
                self.grbl_port = data.get('port')
                logger.info(f"GRBL board detected on port: {self.grbl_port}")
                event_received.set()

        await event_received.wait()
        return True

    def parse_pendant_message(self, message_str):
        """Parse JSON message from pendant microcontroller."""
        try:
            return json.loads(message_str.strip())
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse pendant message: {e}")
            return None

    def _format_jog_gcode(self, pending):
        distance = pending['rt'] * pending['mv'] * 0.001
        return f"$J=G91 {pending['ax']}{distance:.3f} F{JOG_FEED_RATE}"

    def _accumulate_jog(self, pendant_data):
        axis = pendant_data.get('ax')
        ticks = pendant_data.get('mv', 0)
        if not axis or ticks == 0:
            return False

        rate = pendant_data.get('rt', 1)
        if (
            self._jog_pending is not None
            and (self._jog_pending['ax'] != axis or self._jog_pending['rt'] != rate)
        ):
            logger.debug(
                "Jog axis/rate changed while pending; replacing accumulated movement"
            )
            self._jog_pending = None

        if self._jog_pending is None:
            self._jog_pending = {'ax': axis, 'rt': rate, 'mv': ticks}
        else:
            self._jog_pending['mv'] += ticks

        return True

    def _reset_jog_state(self):
        self._jog_pending = None
        self._jog_notify.set()

    def note_pendant_message(self, pendant_data):
        """Record pendant activity and whether a jog flush may be needed."""
        self.last_pendant_message_time = time.monotonic()

        if pendant_data.get('emg'):
            self.jog_flush_pending = False
            return

        axis = pendant_data.get('ax')
        ticks = pendant_data.get('mv', 0)
        if axis and ticks != 0:
            self.jog_flush_pending = True

    async def handle_pendant_message(self, pendant_data):
        self.note_pendant_message(pendant_data)

        if pendant_data.get('emg'):
            self._reset_jog_state()
            logger.info("Emergency stop triggered")
            await self.send_gcode('!')
            return

        if self._accumulate_jog(pendant_data):
            self._jog_notify.set()

    async def jog_send_loop(self):
        """Send jog commands at least 100ms apart, accumulating commands that arrive sooner."""
        while self.running:
            if self._jog_pending:
                # Calculate time until next write is allowed
                current_time = time.monotonic()
                time_since_last_write = current_time - self._last_write_time
                remaining_wait = max(0, COMMAND_WRITE_INTERVAL - time_since_last_write)
                
                if remaining_wait > 0:
                    await asyncio.sleep(remaining_wait)
                
                # Check if we still have a pending jog after the wait
                if self._jog_pending:
                    pending = self._jog_pending
                    self._jog_pending = None
                    gcode = self._format_jog_gcode(pending)
                    try:
                        await self.write_to_grbl(f"{gcode}\n")
                        self._last_write_time = time.monotonic()
                        logger.info(f"Sent G-code: {gcode}")
                    except Exception as e:
                        logger.error(f"Failed to send G-code: {e}")
                        self._jog_pending = pending
                continue

            self._jog_notify.clear()
            try:
                await asyncio.wait_for(
                    self._jog_notify.wait(),
                    timeout=PENDANT_REPORT_INTERVAL,
                )
            except asyncio.TimeoutError:
                pass

    async def read_pendant_data(self):
        """Read and process data from pendant."""
        buffer = ""

        while self.running:
            try:
                if self.serial_connection and self.serial_connection.in_waiting:
                    data = self.serial_connection.read(self.serial_connection.in_waiting).decode('utf-8', errors='ignore')
                    buffer += data

                    # Process complete messages (lines)
                    while '\n' in buffer:
                        line, buffer = buffer.split('\n', 1)
                        message = self.parse_pendant_message(line)

                        if message:
                            await self.handle_pendant_message(message)

                await asyncio.sleep(PENDANT_REPORT_INTERVAL)

            except Exception as e:
                logger.error(f"Error reading pendant data: {e}")
                await asyncio.sleep(PENDANT_REPORT_INTERVAL)

    async def check_jog_idle_flush(self):
        """Flush queued jog commands when the pendant stops reporting."""
        while self.running:
            if (
                self.jog_flush_pending
                and self.last_pendant_message_time is not None
                and time.monotonic() - self.last_pendant_message_time >= self.jog_idle_flush_timeout
            ):
                await self.flush_jog_queue()
                self.jog_flush_pending = False

            await asyncio.sleep(PENDANT_REPORT_INTERVAL)

    async def write_to_grbl(self, data):
        """Send data to GRBL via CNCjs Socket.IO write event."""
        await self.sio.emit('write', (self.grbl_port, data))

    async def send_gcode(self, command):
        """Send G-code command to CNCjs via Socket.IO."""
        try:
            await self.write_to_grbl(f"{command}\n")
            logger.info(f"Sent G-code: {command}")
        except Exception as e:
            logger.error(f"Failed to send G-code: {e}")

    async def flush_jog_queue(self):
        """Cancel GRBL jogging and flush remaining jog commands in CNCjs."""
        self._reset_jog_state()
        try:
            await self.write_to_grbl(GRBL_JOG_CANCEL)
            self._last_write_time = time.monotonic()
            logger.info("Sent jog cancel (0x85) to flush queued jog commands")
        except Exception as e:
            logger.error(f"Failed to flush jog queue: {e}")

    async def run(self):
        """Main run loop for the bridge."""
        # Connect to pendant
        if not await self.connect_serial():
            logger.error("Failed to connect to pendant serial port")
            return

        # Connect to CNCjs
        if not await self.connect_cncjs():
            logger.error("Failed to connect to CNCjs server")
            if self.serial_connection:
                self.serial_connection.close()
            return

        # Wait for GRBL board to be ready
        if not await self.wait_for_grbl_connection():
            logger.error("GRBL board not detected")
            if self.serial_connection:
                self.serial_connection.close()
            await self.sio.disconnect()
            return

        # Start reading pendant data, jog sender, and idle jog flush monitor
        flush_task = asyncio.create_task(self.check_jog_idle_flush())
        jog_task = asyncio.create_task(self.jog_send_loop())
        try:
            await self.read_pendant_data()
        except KeyboardInterrupt:
            logger.info("Bridge stopped by user")
        except Exception as e:
            logger.error(f"Bridge error: {e}")
        finally:
            self.running = False
            flush_task.cancel()
            jog_task.cancel()
            try:
                await flush_task
            except asyncio.CancelledError:
                pass
            try:
                await jog_task
            except asyncio.CancelledError:
                pass
            if self.serial_connection:
                self.serial_connection.close()
            await self.sio.disconnect()


def main():
    parser = argparse.ArgumentParser(
        description='CNCjs Pendant Python Bridge - Bridge a CNC pendant to CNCjs via WebSocket'
    )
    parser.add_argument(
        '-s', '--serial',
        default=DEFAULT_SERIAL_PORT,
        help=f'Serial port device of the pendant microcontroller (default: {DEFAULT_SERIAL_PORT})'
    )
    parser.add_argument(
        '-b', '--baudrate',
        type=int,
        default=DEFAULT_BAUDRATE,
        help=f'Baud rate for serial communication (default: {DEFAULT_BAUDRATE})'
    )
    parser.add_argument(
        '-u', '--url',
        default=DEFAULT_CNCJS_URL,
        help=f'URL of the CNCjs server (default: {DEFAULT_CNCJS_URL})'
    )
    parser.add_argument(
        '-p', '--port',
        type=int,
        default=DEFAULT_CNCJS_PORT,
        help=f'Port of the CNCjs server (default: {DEFAULT_CNCJS_PORT})'
    )
    parser.add_argument(
        '-t', '--jog-flush-timeout',
        type=float,
        default=JOG_IDLE_FLUSH_TIMEOUT,
        help=(
            'Seconds without pendant reports before sending jog cancel (0x85) to flush '
            f'the jog queue (default: {JOG_IDLE_FLUSH_TIMEOUT})'
        )
    )

    args = parser.parse_args()

    logger.info("Starting CNCjs Pendant Python Bridge")
    logger.info(f"Serial port: {args.serial}, Baudrate: {args.baudrate}")
    logger.info(f"CNCjs server: {args.url}:{args.port}")

    # Create and run bridge
    bridge = PendantBridge(
        serial_port=args.serial,
        baudrate=args.baudrate,
        cncjs_url=args.url,
        cncjs_port=args.port,
        jog_idle_flush_timeout=args.jog_flush_timeout,
    )

    asyncio.run(bridge.run())


if __name__ == "__main__":
    main()
