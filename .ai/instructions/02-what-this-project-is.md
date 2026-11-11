## What this project is

Self-hosted FastAPI service that turns an STL/3MF/OBJ upload into a finished print on a Klipper
printer: the container slices with the OrcaSlicer CLI binary, then hands the `.gcode` to Moonraker
over HTTP. One container, no database, no shared filesystem with the printer host.

