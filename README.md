# CNCjs Pendant Python Bridge

Simple Python script for bridging a CNC pendant and the CNCjs Socket.IO interface.  
The script takes arguments for the serial port of the pendant microcontroller, as well as the IP address and port of the CNCjs server.

| Parameter | Description |
|---|---|
| `-s`, `--serial` | Serial port device of the pendant microcontroller. Default is `/dev/ttyACM0`. |
| `-b`, `--baudrate` | Baud rate used to communicate with the pendant microcontroller. Default is `115200`. |
| `-p`, `--port` | Port of the CNCjs server. Default is `8000`. |
| `-u`, `--url` | URL of the CNCjs server. Default is `http://localhost`. |

The software waits until the CNCjs server becomes available in case the server is launched after this application.

Once connected to the CNCjs server, the application automatically detects the serial port of the GRBL board connected to CNCjs and waits until the server is ready to accept G-code commands.

## Build and Execution Instructions

This repository is maintained using the `uv` package management system. After [installing uv](https://docs.astral.sh/uv/getting-started/installation/), you can run the application with:

```bash
uv run main.py
```

For Linux system, you may need to add the user to `dialout` to access serial port.
```bash
sudo usermod -aG dialout $USER
newgrp dialout
```

## CNC Pendant Specification

The CNC pendant contains a microcontroller that reports its current status through serial communication whenever the state changes.

The firmware for the controller is hosted in [this repository](https://github.com/mohammedari/CNC-Pendant-Firmware-for-CNCjs/tree/master).

Each line consists of a JSON object in the following format:

```json
{"ax":"X","rt":1,"mv":-8,"emg":0}
```

| Name | Value | Description |
|---|---|---|
| `ax` | `['X','Y','Z','U','V','W']` | Selected axis |
| `rt` | `[1, 10, 100]` | Distance multiplier selection |
| `mv` | 32-bit signed integer | Jog dial movement (in ticks) since the last report |
| `emg` | `[0, 1]` | Emergency stop switch status |

The controller reports its status whenever the jog dial is rotated or the emergency stop switch is pressed.

While the emergency stop switch is active, reports are periodically sent at the minimum report interval.

The minimum report interval is `20ms`. Jog dial movement occurring within the interval is accumulated and reported in the next message.

## CNCjs Specification

This application maps the reported pendant status to G-code commands and sends them to the CNCjs server.

Each received report line is translated into a single G-code command. For reports with non-zero jog dial movement, a `$J=G91` jog command is sent to the CNCjs server.

The jog dial movement (`mv`) is interpreted as `0.001mm` per tick and multiplied by the selected distance multiplier (`rt`). Therefore, the total movement specified in the generated G-code command is:

```text
rt * mv * 0.001mm
```

This application sends the following G-code commands:

| G-code Command Example | Description |
|---|---|
| `$J=G91 X1 F1000` | Jog command sent for reports with non-zero movement. The command consists of the selected axis (`X`, `Y`, `Z`, etc.), the calculated movement amount, and a fixed feed rate of `F1000`. |
| `!` | Every report with `emg=1` is mapped to the GRBL feed hold / emergency stop command. |

The application also listens to CNCjs Socket.IO events to handle GRBL board connections.

Once a `serialport:change` event is detected, the application stores the name of the connected serial port.
While the connection remains active, all G-code commands are sent to the most recently connected board.
