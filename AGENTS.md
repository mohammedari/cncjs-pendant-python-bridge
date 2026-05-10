# AGENTS.md

## Development Workflow

Development should refer to GitHub issues, implement the content written in the issues, and create a PR.

### Branching Strategy
- **Feature branches**: Create branches starting with `feature/` for new features or enhancements
- **Development flow**: 
  1. Create a `feature/` branch from `master`
  2. Implement the feature referring to the GitHub issue
  3. Push the branch to GitHub
  4. Create a PR to merge into `master`
- **Direct pushes to main/master are prohibited**: All changes must go through PR review

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