import argparse
import asyncio
import json
import logging
import serial
import time
import websockets
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


class PendantBridge:
    def __init__(self, serial_port, baudrate, cncjs_url, cncjs_port):
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.cncjs_url = cncjs_url
        self.cncjs_port = cncjs_port
        self.serial_connection = None
        self.websocket = None
        self.grbl_port = None
        self.running = True

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
        """Connect to CNCjs server via WebSocket."""
        ws_url = f"ws://{self.cncjs_url.replace('http://', '').replace('https://', '')}:{self.cncjs_port}"
        max_retries = 30
        retry_count = 0

        while retry_count < max_retries:
            try:
                self.websocket = await websockets.connect(ws_url)
                logger.info(f"Connected to CNCjs server at {ws_url}")
                return True
            except Exception as e:
                retry_count += 1
                logger.warning(f"Failed to connect to CNCjs (attempt {retry_count}/{max_retries}): {e}")
                await asyncio.sleep(1)

        logger.error(f"Failed to connect to CNCjs after {max_retries} attempts")
        return False

    async def wait_for_grbl_connection(self):
        """Wait for GRBL board to be ready on CNCjs."""
        try:
            async for message in self.websocket:
                data = json.loads(message)
                # Check if GRBL controller is connected
                if data.get('type') == 'controller:state':
                    controller_state = data.get('payload', {})
                    if controller_state.get('port'):
                        self.grbl_port = controller_state['port']
                        logger.info(f"GRBL board detected on port: {self.grbl_port}")
                        return True
        except Exception as e:
            logger.error(f"Error waiting for GRBL connection: {e}")
        return False

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
                            gcode_commands = self.pendant_to_gcode(message)
                            for cmd in gcode_commands:
                                await self.send_gcode(cmd)

                await asyncio.sleep(PENDANT_REPORT_INTERVAL)

            except Exception as e:
                logger.error(f"Error reading pendant data: {e}")
                await asyncio.sleep(PENDANT_REPORT_INTERVAL)

    async def send_gcode(self, command):
        """Send G-code command to CNCjs via WebSocket."""
        try:
            if self.websocket:
                message = {
                    'id': str(datetime.now().timestamp()),
                    'jsonrpc': '2.0',
                    'method': 'gcode',
                    'params': [command]
                }
                await self.websocket.send(json.dumps(message))
                logger.info(f"Sent G-code: {command}")
        except Exception as e:
            logger.error(f"Failed to send G-code: {e}")

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
            await self.websocket.close()
            return

        # Start reading pendant data
        try:
            await self.read_pendant_data()
        except KeyboardInterrupt:
            logger.info("Bridge stopped by user")
        except Exception as e:
            logger.error(f"Bridge error: {e}")
        finally:
            self.running = False
            if self.serial_connection:
                self.serial_connection.close()
            if self.websocket:
                await self.websocket.close()


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

    args = parser.parse_args()

    logger.info("Starting CNCjs Pendant Python Bridge")
    logger.info(f"Serial port: {args.serial}, Baudrate: {args.baudrate}")
    logger.info(f"CNCjs server: {args.url}:{args.port}")

    # Create and run bridge
    bridge = PendantBridge(
        serial_port=args.serial,
        baudrate=args.baudrate,
        cncjs_url=args.url,
        cncjs_port=args.port
    )

    asyncio.run(bridge.run())


if __name__ == "__main__":
    main()
