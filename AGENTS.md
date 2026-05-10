# AGENTS.md

## Development Workflow

Development should refer to GitHub issues, implement the content written in the issues, and create a PR.

## Project Overview

This is a Python script that bridges a CNC pendant microcontroller to the CNCjs websocket interface. It reads serial input from the pendant and sends corresponding G-code commands to CNCjs.

## Build and Run

Use `uv run main.py` to run the application. See [README.md](README.md) for full build instructions and CLI parameters.

## Architecture

- Reads JSON messages from pendant serial port
- Converts jog movements to `$J=G91` G-code commands
- Sends commands via WebSocket to CNCjs server
- Auto-detects GRBL board serial port
- Waits for CNCjs server availability

## Key Conventions

- Python >= 3.14 required
- Use `uv` for package management
- Pendant report interval: minimum 20ms
- Fixed jog feed rate: F1000
- Emergency stop: `!` command

## Potential Pitfalls

- Ensure CNCjs server is running before starting the bridge
- Serial port permissions may require user setup
- Wait for GRBL board connection before sending commands