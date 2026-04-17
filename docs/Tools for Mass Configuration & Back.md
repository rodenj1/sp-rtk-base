Tools for Mass Configuration & Backup of u-blox GNSS Receivers (Excluding u-center)
Modern u-blox GNSS receivers (like the ZED-F9P, ZED-X20, etc.) can be configured and managed using various tools that support bulk configuration, backup/restore of settings, configuration file comparison, and automated parameter setting. Below is a summary of key tools (excluding u-blox’s GUI u-center) that we researched for these purposes, each with their source, primary uses, basic usage, pros/cons, and documentation links.
1. PyUBXUtils – Python CLI Utilities for u-blox Config
Source/Location: Semuconsulting’s PyUBXUtils (open-source on GitHub; also installable via PyPI as pyubxutils).
Primary Use Cases: Comprehensive backup and restore of device configuration, applying saved configs to devices, comparing configuration files, and setting specific parameter values or message rates. It’s essentially a suite of command-line tools designed specifically for u-blox’s generation 9 and above GNSS modules (e.g. NEO-M9N, ZED-F9P, ZED-F9R, ZED-X20). Key utilities include: [github.com]

ubxsave – Polls the receiver for all configuration items and saves the full config to a file (binary .ubx or optionally text). This serves as a complete backup of the device’s settings. [github.com]
ubxload – Loads a previously saved config file into a receiver, restoring all settings in one transaction. Ensures atomic application: if any setting is incompatible, the load is aborted for consistency. [github.com]
ubxcompare – Compares two or more config files (either u-center text format or .ubx binary) and highlights differences. Useful for verifying changes between devices or firmware versions. [github.com]
ubxsetrate – Sets the output rate of specific UBX or NMEA messages on the receiver (e.g., enable/disable periodic messages). [github.com]
ubxbase – Convenience tool to configure a receiver as an RTK base station (setting survey-in or fixed base mode) with one command.

How to Use: Install the pyubxutils package, then run these tools from a terminal (they are CLI executables). For example:

To backup a device on COM3: ubxsave --port COM3 --baudrate 115200 --outfile myConfig.ubx (adjust port/baud as needed). This saves all current settings from the GNSS to myConfig.ubx.
To restore that config to another device: ubxload --port COM3 --baudrate 115200 --infile myConfig.ubx. The tool will apply all settings; if any fail (e.g., due to incompatibility), no changes are made (it’s all-or-nothing). [github.com]
To compare two config files (e.g., default.txt vs tweaked.txt from u-center): ubxcompare file1.txt file2.txt. The output will list keys that differ and their values in each file for side-by-side review.
To quickly change a message rate: e.g., ubxsetrate --port COM3 --msgClass 0x01 --msgID 0x07 --rate 0 would disable the UBX-NAV-PVT message (Class 0x01, ID 0x07) on that port. You can also specify message names or use presets like --msgClass allubx to adjust groups of messages.

Pros:

Automation-friendly: Being CLI-based, PyUBXUtils is easily scriptable for mass configuration (you can loop through multiple devices with a script calling these utilities). It’s cross-platform (runs on Windows, Linux, etc., anywhere Python 3 runs). [github.com]
Complete coverage of settings: Uses u-blox’s modern config interface (VALGET/VALSET), so it can capture and restore every configuration item on Gen9+ devices (covering even advanced and port settings). The backup and restore are thorough and much faster than manual methods.
Safe and atomic: The restore (ubxload) applies all settings in a single grouped transaction. If any sub-command fails (e.g., not supported on target), the device rejects the whole set, preventing partial application. [github.com]
Comparison capability: ubxcompare makes it straightforward to see differences between configurations (e.g., between two receivers or before/after changes) without manually diffing long text files.
Open-source & documented: The tools are well-documented (see the PyUBXUtils README and PyPI project description) and maintained alongside u-blox updates.

Cons:

Gen9+ only for full config: The backup/load functionality is limited to newer u-blox generations that support the CFG-VALGET/VALSET mechanism (ZED-F9P and later, and M9 series). Older devices (like Ublox 8 or earlier) cannot be fully backed up with ubxsave, since they don’t expose all settings as config items. (They can still be configured by other means, but these utilities focus on modern receivers.) [github.com]
Requires Python environment: Using these tools means installing Python and the package. This is straightforward for many, but is an extra step compared to a standalone binary. However, a positive flip side is that you can embed these in Python scripts if needed.
No multi-device simultaneous support out-of-the-box: Each invocation handles one connection. To configure many devices in parallel, you’d need to run multiple instances or script the process (which is feasible, but there’s no single command that configures a batch of receivers at once).

Documentation/Examples: The PyUBXUtils PyPI page contains usage examples and detailed explanations for each sub-tool (including all command-line options). The GitHub README provides a quick overview of each utility and notes (e.g., generation limitations). For instance, documentation notes that if ubxsave encounters timeouts, you can increase --waittime to accommodate busy devices. These resources also show sample outputs and discuss how the utilities work under the hood (built on the PyUBX2 library, described below). [github.com], [github.com] [github.com]

2. GPSD ubxtool – Linux Command-Line for UBX Config
Source/Location: Part of the open-source GPSD project (commonly available on Linux; included in gpsd-clients packages). Documentation is on the GPSD website and in man pages.
Primary Use Cases: Sending configuration commands and queries to u-blox receivers via command line. ubxtool allows you to script changes to the receiver’s settings without a GUI, which is ideal for remote or automated setups. Typical uses include: resetting the device to factory defaults, changing update rates, enabling or disabling specific NMEA/UBX message types, switching constellations on/off, and saving configuration to the device’s non-volatile memory. It can also poll for information (e.g., firmware version, current config of a setting) and read outputs. Essentially, ubxtool is a Swiss-army knife to exercise the UBX protocol for both older (Gen8) and newer (Gen9/Gen10) u-blox modules. [manpages.ubuntu.com]
How to Use: ubxtool runs in a terminal. You can point it either directly to a serial device or have it communicate through a running gpsd daemon. Basic usage patterns:

Direct device usage: For example, to directly open a serial port and send a command, you might run:
Shellubxtool -f /dev/ttyACM0 -s 115200 -P 27 -p MON-VERShow more lines
This would connect to /dev/ttyACM0 at 115200 baud, assume protocol version 27 (ZED-F9P uses UBX ProtVer ~27+), and poll the MON-VER message (which returns version info). You should see the receiver’s software/hardware version output.
Via gpsd: If gpsd is managing the device, you can simply use ubxtool without specifying -f, and it will talk to the gpsd service. E.g., if gpsd is running and you want to set the dynamic model to “Stationary”, you could do:
Shellubxtool -P 27 -p CFG-NAV5,0,2,0,0,0,0,0,0,0Show more lines
(This sends a UBX-CFG-NAV5 command with parameters for Stationary mode – not very human-friendly, but demonstrates sending a config by message name and payload.) Often, though, ubxtool provides shortcuts for common tasks which avoid crafting hex bytes manually.
Enabling/Disabling message streams: There are convenient flags like -e (enable) and -d (disable) for well-known groups. For example:

ubxtool -d NMEA will disable all basic NMEA output messages, so the receiver stops sending NMEA sentences. [manpages.ubuntu.com]
ubxtool -e BINARY will enable the UBX binary messages (it’s equivalent to turning on the UBX protocol out). These can be used after a factory reset to ensure the desired protocol is active.


Resetting to defaults: ubxtool -p RESET issues a configuration reset (UBX-CFG-CFG with clear/save/default flags) to restore factory settings (this is shorthand in recent gpsd versions). After doing this, you might use the -e/-d flags to quickly set up the output protocols you want (e.g., enable UBX only).
Using Configuration Items: On modern receivers (protocol v27+), you can get/set individual config key-values. For example:

ubxtool -g CFG-GNSS will retrieve the current GNSS constellation settings (returns the configuration structure for enabled GNSS systems).
ubxtool -x CFG_RATE_MEAS,200 would set the measurement rate to 200ms (5 Hz). Here -x sets a config item by name and value. You need to specify -P with the proper protocol version so it knows to use the new interface.



Pros:

Flexible and scriptable: It’s purely command-line, making it easy to call from scripts (bash, etc.) on Linux. You can incorporate ubxtool calls in init scripts or cron jobs to reconfigure devices remotely.
Covers old and new methods: Supports legacy UBX messages and the newer config item interface. In fact, over 600 configuration Key Names are recognized by ubxtool for Gen9+ devices, allowing human-readable access to settings (you don’t need to remember key IDs). This means you can script changes to almost any parameter exposed by the receiver’s config interface.
Real-time interaction: You can use it on a live system to tweak settings on the fly or query status without stopping the data stream. (For instance, toggling a message or adjusting power mode while other software is using the GPS.)
Lightweight: No GUI or heavy dependencies (aside from gpsd itself). It’s convenient for headless or embedded Linux environments where installing a full GUI (like u-center) isn’t practical.
Documentation & examples: The GPSD ubxtool examples page provides many “recipes” for common tasks, from basic (changing baud rate) to advanced (survey-in configuration). This helps mitigate the learning curve by giving copy-pastable commands for typical scenarios.

Cons:

Steep learning curve: ubxtool operates at the low-level UBX protocol. As the manual states, you must be familiar with u-blox’s command documentation to use it effectively. Many operations require knowing message or key names and sometimes composing comma-separated parameters or hex payloads. Beginners might find it daunting compared to a GUI. [manpages.ubuntu.com]
GPSD dependency (for some uses): If your environment isn’t already using gpsd, invoking ubxtool directly can still work (-f /dev/ttyXXX), but some functionality (like using shared gpsd data) assumes gpsd is running. On systems where gpsd is not desired, you can still use it standalone, but the documentation mostly targets gpsd-managed scenarios.
No bulk “download/upload” of config: Unlike some tools, ubxtool does not have a single command to save all settings to a file or apply a whole config file. You have to script individual commands for each setting you care about. (In practice, one might use ubxtool -g on a bunch of items to create a snapshot script, but it’s manual.) It’s powerful for tweaking or automation, but not intended for one-click full backup/restore – that would require multiple steps or using another tool like PyUBXUtils.
A bit verbose/not streamlined for comparison: To compare configurations between two receivers, you’d have to poll each relevant item with -g and diff the outputs yourself. ubxtool doesn’t directly compare configs (again, not its focus).

Documentation/Examples: Official reference is the ubxtool(1) man page and the GPSD project’s example guide. These outline all options; for example, the man page explains options like -c (send raw command by hex), -e/-d toggles, -P for protocol version, -g (get config by key), -x (set config item). The examples guide is particularly useful, showing recipes such as disabling NMEA output, changing the dynamic model, saving and restoring survey-in mode, etc., with both legacy and new commands. If you’re scripting on Linux, checking these resources will accelerate the learning process. [manpages.ubuntu.com]

3. PyUBX2 – Python Library for UBX Protocol
Source/Location: Semuconsulting’s PyUBX2 library (open-source on GitHub, available via PyPI).
Primary Use Cases: PyUBX2 is a Python API for u-blox’s UBX protocol, used when you want to create custom programs or scripts that interact with GNSS receivers. Instead of a fixed tool, it’s a library that allows you to parse incoming data, construct and send configuration messages, and integrate GNSS configuration/control into larger Python applications. Use cases include: writing an automated setup script that configures a receiver on startup, building a logging service that also ensures certain settings are applied, or any scenario where you need programmatic control (for example, toggling settings based on external conditions, or coordinating multiple receivers). PyUBX2 also parses NMEA and RTCM3 messages (via companion libs), but for configuration our focus is on its UBX message capabilities. [pypi.org]
How to Use: Write a Python script utilizing the library classes. Basic outline:

Connecting to the receiver: Use serial.Serial (from PySerial) to open the device’s COM port (or use a network socket). Then create a UBXReader to read messages. For example:
Pythonimport serialfrom pyubx2 import UBXReaderstream = serial.Serial('COM3', 115200, timeout=1)  # open serial portubr = UBXReader(stream)raw_data, parsed_data = ubr.read()  # read one messageShow more lines
The UBXReader gives you parsed message objects continuously, which you can use to monitor ACKs or incoming navigation data.
Sending configuration commands: PyUBX2 provides a UBXMessage class to build messages. You typically call UBXMessage(..., **params) with the message class/id and the parameters. For example, to set a configuration key (Gen9+ method), you could do:
Pythonfrom pyubx2 import UBXMessage# Example: Set dynamic model (CFG-NAVSPG-DYNMODEL) to 4 (Automotive)msg = UBXMessage("CFG", "CFG-NAV5", SET, dynModel=4, mask=1)stream.write(msg.serialize())Show more lines
This constructs a UBX CFG-NAV5 (navigation engine settings) message in “SET” mode with the dynamic model field set. Similarly, you can create a message to save config to flash (UBX-CFG-CFG with appropriate mask), or poll a configuration (by using POLL instead of SET). PyUBX2 internally knows how to format and checksum the message. [pypi.org]
Parsing responses and data: Any bytes coming from the receiver can be fed to UBXReader to decode. For instance, after sending a config, you may want to read until an ACK is received:
Pythonraw, parsed = ubr.read()if parsed and parsed.identity == "ACK-NAK":    ... handle NAK (failure) ...elif parsed and parsed.identity == "ACK-ACK":    ... success ...Show more lines
You can also enable the reader to parse NMEA and RTCM3 if needed, or filter only UBX.
Integration: Because it’s a library, you can integrate GNSS configuration with other tasks. For example, you could write a script to query a web API, then configure the receiver accordingly, or log data and react to it in real time (e.g., automatically turn on an LED when position is fixed).

Pros:

Extremely powerful & flexible: PyUBX2 essentially gives you full programmatic control over the receiver. Anything you can do with UBX protocol, you can implement with this library – from simple settings to complex interactive sessions. It’s up-to-date with the latest u-blox messages and configuration keys (covering all current devices as of its latest release). [pypi.org]
Covers all message types: Not just config – it can parse or generate UBX, NMEA, and RTCM messages. If your workflow involves listening for certain NMEA sentences or forwarding RTCM corrections, you can handle that in the same code. [pypi.org]
Integrates with automation workflows: Because it’s Python, it can be combined with other libraries (for logging, networking, GUIs, etc.). For example, one could use PyUBX2 to make a web dashboard that both displays GNSS data and lets you send config commands from a webpage. This is harder to achieve with standalone CLI tools.
Good documentation and support: The PyUBX2 documentation includes examples for reading, writing, and using the configuration interface. There’s also a built-in simple command-line utility (ubxdump) and a GUI client (PyGPSClient) for testing, which demonstrate use of the library. The project is actively maintained (as of 2024) by the author, who responds to issues on GitHub. [pypi.org], [pypi.org]
No external dependencies beyond pyserial: It’s lightweight to include in projects.

Cons:

Requires programming knowledge: Unlike the other tools, PyUBX2 is not a ready-made application but a toolkit. To use it, you need to write Python code. This offers maximum flexibility, but if you just want a quick one-off backup or toggle, writing a script might be more effort than using a CLI tool.
Higher initial effort for simple tasks: For instance, performing a full backup with PyUBX2 means writing a loop to poll all config items – something ubxsave does out-of-the-box. So, for tasks that are already solved by PyUBXUtils or ubxtool, using those might be faster. PyUBX2 shines when you have custom or conditional logic that the canned tools can’t do.
No built-in config comparison: You would have to retrieve config data and then manually compare in code or output to files and diff them. (However, you could use PyUBX2 to build your own comparison function, if so inclined.)
Limited to environments where Python can run: Typically not an issue on desktops or Raspberry Pi-class devices, but if you’re in a very constrained environment without Python, you’d use other means.

Documentation/Examples: See the PyUBX2 project page for full documentation. The snippet below from the PyUBX2 description highlights its broad device support and message coverage: “The library implements a comprehensive set of inbound (SET/POLL) and outbound (GET) messages for all current u-blox GPS/GNSS devices…”. The repo’s examples directory has scripts for common tasks (like parsing a log file, or sending specific UBX commands). Additionally, the PyGPSClient GUI (from the same author) can be used as a reference on how to use PyUBX2 in a larger application (it uses PyUBX2 to handle all UBX communications under the hood). [pypi.org]

4. RTKLIB Utilities (e.g. STR2STR) – GNSS Streaming with Config Commands
Source/Location: RTKLIB – an open-source GNSS toolkit by Tomoji Takasu (available on http://rtklib.com and various repos). Relevant tools for our context are command-line programs like str2str (stream server) and rtkrcv (RTK receiver daemon).
Primary Use Cases: RTKLIB is primarily for processing GNSS data (RTK, logging, format conversion), but its streaming tool str2str can also send initialization commands to a receiver. The typical use case here is: you have a u-blox device feeding data into an RTKLIB setup, and you want RTKLIB to automatically configure that device at startup (for example, enable raw measurement output, set the update rate, etc.). So while RTKLIB isn’t a dedicated config management tool, it supports applying a set of config commands whenever a connection is made, which can achieve mass configuration when deploying receivers in the field. Additionally, RTKLIB can log raw data for backups of observation data (not configuration), and distribute data to multiple endpoints.
How to Use: Focus on str2str, since it’s most relevant for sending commands:

Command file (-c option): You can prepare a text file containing UBX commands (in either plain ASCII or hex) that you want to send to the receiver on startup. Then run str2str with the -c option pointing to this file. For example: [manpages.ubuntu.com]
str2str -in serial://ttyACM0:460800#ubx -out file://data.ubx -c ubxcmds.txt

This would open the u-blox on ttyACM0 at 460800 baud, start logging all data to data.ubx, and immediately read and send the commands in ubxcmds.txt to the receiver. The command file can contain lines like:
!UBX CFG, 0x09, 0x00, ...    (example hex or text for a UBX command)

Typically, one uses u-center or documentation to generate the right UBX commands for desired settings, then places them in the file. Common uses are enabling RAWX/SFRBX messages (for raw data logging), setting the navigation rate, and turning off unwanted outputs.
Multiple output streams: str2str can take one input and fork it to multiple outputs (e.g., send to an NTRIP caster while also saving to a file, etc.). This doesn’t configure the receiver per se, but is useful if you need to share one receiver’s data with many clients. It can act like a mini-server for the GNSS data.
Reserving config vs permanent config: Note that commands sent via str2str -c are by default just volatile settings (they apply to the running receiver session). If you need them saved in flash, you’d include a save command (UBX-CFG-CFG) in the commands file as well. In many cases, though, you don’t need to permanently alter the device – you just need it to output certain messages while the logging/streaming is active, which this method handles.

Pros:

Combines configuration with operation: In deployments where you are already using RTKLIB (for RTK or logging), the ability to configure the device as part of the stream startup means you don’t need a separate step or tool for config. It’s very convenient: just drop the UBX commands in a file and let str2str apply them every time it connects to the device. [manpages.ubuntu.com]
Automation & scaling: You can set up identical command files on multiple devices/machines, and whenever they run str2str, all receivers get the same config without manual intervention. This is good for mass configuration in a distributed sense – e.g., 10 rovers in the field each running an identical script to configure themselves on boot and start streaming data.
Multi-platform: RTKLIB is available for Windows and Linux. You can use str2str on a PC, Raspberry Pi, or even some controllers that support the required libraries.
Logging and streaming capabilities: In addition to config, you get the benefit of RTKLIB’s robust streaming. For instance, you can log the raw data for later analysis (which can be considered a form of backup of the device’s output). If you needed to verify configurations, having a log of the UBX messages (including ACKs) is useful. RTKLIB also has options to output decoded solutions, but that’s beyond config scope.
Advanced features: str2str can do things like format conversion on the fly and act as an NTRIP server. So if your “mass configuration” initiative is part of a larger GNSS data system, RTKLIB’s tools might fit well.

Cons:

Not a dedicated config tool: There’s no functionality to retrieve a config or compare configs. RTKLIB won’t help you generate a config file; you have to supply one. It doesn’t interface with u-blox’s config interface directly (you just provide the raw UBX commands). So you likely still need another method (like u-center or PyUBXUtils) to initially create those commands or files.
Learning/crafting commands: Creating the command file can be tricky if you’re not already familiar with UBX messages. RTKLIB doesn’t provide shortcuts or symbolic names – you write the bytes or use the slightly arcane “!UBX CFG, ” syntax. It’s not as user-friendly as PyUBXUtils in that regard.
No error feedback to config changes: When str2str sends the commands, you don’t get a clear structured report of success or failure (aside from maybe seeing ACK/NAK in the console output if running verbose). There’s no built-in re-try or confirmation mechanism, so you have to trust the commands (or manually inspect logs). In critical systems, you might still do a verification step via another tool or code.
Primarily for runtime use: If the goal is to permanently configure devices in bulk and then disconnect them, RTKLIB is not the first choice. It shines when the receiver is part of an always-running service (like an RTK base) and you need it configured each time that service runs. For one-time setups or updates, a different tool might be more straightforward.

Documentation/Examples: The https://rtklib.com and man pages describe the -c (commands file) option of str2str. For instance, the Debian manpage excerpt shows -c file as “receiver commands file” which is exactly the feature allowing automated config on connect. RTKLIB forums and tutorials (like Rtklibexplorer’s blog) provide examples of command files for u-blox (e.g., enabling raw outputs on an M8T by sending specific UBX messages). If your project is using RTKLIB, leveraging str2str -c is a proven approach to ensure your u-blox receivers initialize correctly every time. [manpages.ubuntu.com]

Additional Notes: Other specialized tools exist (for example, community scripts or vendor-specific utilities), but the ones above are the most general and automation-friendly for u-blox receiver configuration and management. Between them, you can accomplish most tasks: PyUBXUtils for straightforward full backups/restores and batch operations, GPSD’s ubxtool for on-the-fly or Linux-integrated tweaks, PyUBX2 for custom scripting and complex logic, and RTKLIB for embedding config in streaming/logging workflows.

Comparison of Key Tools
The table below provides a quick overview of the above tools and their capabilities:








































Tool & SourceKey CapabilitiesAutomation & ScriptingProsConsPyUBXUtils(Semuconsulting – GitHub repo, PyPI package)Full config backup (ubxsave); Config restore (ubxload); Compare configs (ubxcompare); Set message rates (ubxsetrate); plus RTK base setup (ubxbase). All via CLI commands [github.com].Yes – designed as CLI tools, easy to call in scripts or batch for multiple devices.Complete snapshot/restore of settings (Gen9+ devices) [github.com]; one-command execution for tasks; cross-platform Python-based; excellent for bulk operations.Gen9+ only for full backup/restore [github.com]; requires installing Python and package; not pre-installed on systems.GPSD ubxtool(GPSD project – included in Linux gpsd-clients, Documentation)Send UBX commands (by name or hex) to configure device (reset, change settings); Poll info (e.g., version, config values); Enable/disable message streams (-e/-d flags) [manpages.ubuntu.com]; Supports new Config Items (get/set by key name).Yes – pure CLI. Can be run in scripts (requires gpsd service or direct device access).Very versatile (supports 600+ config key names); ideal for headless setup and dynamic reconfiguration; part of a standard Linux GNSS toolset.Steep learning curve (low-level UBX knowledge needed) [manpages.ubuntu.com]; no one-shot backup/load – must script each setting; primarily Linux (no native Windows version of ubxtool).PyUBX2 Library(Semuconsulting – GitHub, PyPI)Programmatic access to all UBX config messages (build and send UBX commands in Python) [pypi.org]; Parse responses (ACK/NAK, etc.); Handle NMEA/RTCM as well for integrated solutions [pypi.org]. Essentially an API to create custom config and control scripts/apps.Yes – but via writing Python code. Great for automated systems with logic (not just static scripts).Highly flexible and up-to-date (covers all current u-blox messages/protocols) [pypi.org]; integrate GNSS control with other software (one codebase for monitoring and config); cross-platform.Requires Python coding; not a turnkey tool (more development effort for simple tasks); no built-in config diff or batch-save (those can be 