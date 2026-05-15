import argparse
import asyncio
import json
import logging
import serial
import time
import socketio
import requests
from datetime import datetime

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

    def pendant_to_gcode(self, pendant_data):
        """Convert pendant data to G-code commands."""
        commands = []

        # Check for emergency stop
        if pendant_data.get('emg'):
            commands.append('!')
            logger.info("Emergency stop triggered")
            return commands

        # Process jog commands
        axis = pendant_data.get('ax')
        ticks = pendant_data.get('mv', 0)

        if axis and ticks != 0:
            # Calculate distance: rate × ticks × 0.001mm
            rate = pendant_data.get('rt', 1)
            distance = rate * ticks * 0.001

            # Format G-code command: $J=G91 Xn Yn Zn Fn
            gcode = f"$J=G91 {axis}{distance:.3f} F{JOG_FEED_RATE}"
            commands.append(gcode)
            logger.debug(f"Generated G-code: {gcode}")

        return commands

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
                            self.note_pendant_message(message)
                            gcode_commands = self.pendant_to_gcode(message)
                            for cmd in gcode_commands:
                                await self.send_gcode(cmd)

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

    async def send_gcode(self, command):
        """Send G-code command to CNCjs via Socket.IO."""
        try:
            await self.sio.emit('write', (self.grbl_port, f"{command}\n"))
            logger.info(f"Sent G-code: {command}")
        except Exception as e:
            logger.error(f"Failed to send G-code: {e}")

    async def flush_jog_queue(self):
        """Cancel GRBL jogging and flush remaining jog commands in CNCjs."""
        try:
            await self.sio.emit('command', (self.grbl_port, 'jogCancel'))
            logger.info("Sent jogCancel to flush queued jog commands")
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

        # Start reading pendant data and idle jog flush monitor
        flush_task = asyncio.create_task(self.check_jog_idle_flush())
        try:
            await self.read_pendant_data()
        except KeyboardInterrupt:
            logger.info("Bridge stopped by user")
        except Exception as e:
            logger.error(f"Bridge error: {e}")
        finally:
            self.running = False
            flush_task.cancel()
            try:
                await flush_task
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
            'Seconds without pendant reports before sending jogCancel to flush '
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
