# calibrationautomation
Collin Laconto

Portfolio of scripts and automation data for calibration devices

Used my brain and (a lot of) Claude AI

Probe sync is used to take reference probe data recorded on an Additel 286
and plot it on a line graph as well as plotting device under test data from data logger exports.
The goal is to plot all test points on the same time scale to compare sample values at certain timestamps.
Since the Additel records samples every 4 seconds and data loggers are usually every minute,
this allows the user to minimize the data noise produced by taking samples every 4 seconds through a visual line graph.

The device crawler is used to manually navigate through an Additel 286 file tree.
This can be used to find certain files within /Windows or run commands directly.

The device probe script sends a bunch of possible SCPI commands to an Additel device.
Any command supported by the device will output a response that can be viewed.
This script is used to find supported commands depending on the Additel device.