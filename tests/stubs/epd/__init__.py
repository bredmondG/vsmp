# Stub `epd` package so vsmp.py can be imported off-Pi.
#
# The real epd/epdconfig.py reads /proc/cpuinfo at import time to pick a GPIO
# backend and raises RuntimeError on anything that is not a Raspberry Pi, so
# `import vsmp` fails on a development machine without this.
