#!/usr/bin/env python3
"""
InTime Widget - Production-ready time display overlay for Hyprland
A transparent, always-on-top digital clock with multiple display modes
"""

# Disable GTK theme to ensure transparency works
import os
os.environ['GTK_THEME'] = ''

# CRITICAL: Load libgtk4-layer-shell before GTK to ensure proper initialization
# Without this, the widget will open as a regular window instead of an overlay
from ctypes import CDLL
CDLL('libgtk4-layer-shell.so')

import gi
import json
import argparse
import re
import math
import random
import socket
import signal
import sys
from datetime import datetime, timedelta

# Import screen color monitor
from screen_color_monitor import ScreenColorMonitor, HybridColorProcessor

gi.require_version('Gtk', '4.0')
gi.require_version('Gtk4LayerShell', '1.0')

from gi.repository import Gtk, GLib, Pango, PangoCairo
import gi.repository.Gtk4LayerShell as LayerShell
import cairo


class IPCServer:
    """Unix socket server for inter-process communication"""
    def __init__(self, socket_path='/tmp/intime_widget.sock'):
        self.socket_path = socket_path
        self.server_socket = None
        self.callbacks = {}

    def register_callback(self, command, callback):
        """Register a callback function for a command"""
        self.callbacks[command] = callback

    def start(self):
        """Start the IPC server"""
        # Remove existing socket file if it exists
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

        # Create Unix domain socket
        self.server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server_socket.bind(self.socket_path)
        self.server_socket.listen(5)
        self.server_socket.setblocking(False)

        # Make socket readable by all users
        os.chmod(self.socket_path, 0o666)

        # Integrate with GTK main loop
        GLib.io_add_watch(
            self.server_socket.fileno(),
            GLib.IO_IN,
            self._on_incoming_connection
        )

        print(f"IPC server listening on {self.socket_path}")

    def _on_incoming_connection(self, fd, condition):
        """Handle incoming connection"""
        try:
            client_socket, _ = self.server_socket.accept()
            client_socket.setblocking(False)

            # Add watch for client data
            GLib.io_add_watch(
                client_socket.fileno(),
                GLib.IO_IN,
                self._on_client_data,
                client_socket
            )
        except Exception as e:
            print(f"Error accepting connection: {e}")

        return True  # Continue watching

    def _on_client_data(self, fd, condition, client_socket):
        """Handle data from client"""
        try:
            data = client_socket.recv(1024).decode('utf-8').strip()

            if not data:
                client_socket.close()
                return False

            # Parse command (format: "command:arg1:arg2:...")
            parts = data.split(':', 1)
            command = parts[0]
            args = parts[1] if len(parts) > 1 else None

            # Execute callback if registered
            if command in self.callbacks:
                result = self.callbacks[command](args)
                response = f"OK:{result}\n" if result else "OK\n"
            else:
                response = f"ERROR:Unknown command '{command}'\n"

            client_socket.send(response.encode('utf-8'))
            client_socket.close()

        except Exception as e:
            print(f"Error handling client data: {e}")
            try:
                client_socket.send(f"ERROR:{str(e)}\n".encode('utf-8'))
                client_socket.close()
            except:
                pass

        return False  # Remove watch after handling

    def stop(self):
        """Stop the IPC server"""
        if self.server_socket:
            self.server_socket.close()
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)


def parse_duration(duration_str):
    """
    Parse duration string like '30m', '1h', '1h30m', '45s' into total seconds
    Returns: int (total seconds)
    """
    if not duration_str:
        return 0

    # Pattern to match hours, minutes, seconds
    pattern = r'(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?'
    match = re.match(pattern, duration_str.strip())

    if not match:
        raise ValueError(f"Invalid duration format: {duration_str}. Use format like '30m', '1h', '1h30m45s'")

    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)

    if hours == 0 and minutes == 0 and seconds == 0:
        raise ValueError(f"Duration must be greater than 0: {duration_str}")

    return hours * 3600 + minutes * 60 + seconds

def parse_args():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(
        description='InTime Widget - Multi-mode transparent overlay',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                              # Default: show current time
  %(prog)s --mode clock                 # Show current time
  %(prog)s --style lightbulb            # Clock with 3 Body Problem sci-fi glow
  %(prog)s --mode countdown --duration 30m    # 30 minute countdown
  %(prog)s --mode countdown --duration 1h30m  # 1.5 hour countdown
  %(prog)s --mode midnight              # Countdown to midnight
  %(prog)s --color "#FF0000"            # Red color
  %(prog)s --mode countdown --duration 5m --color "#00FF00"  # 5min green countdown
  %(prog)s --position top               # Position at top
  %(prog)s --position-x 100 --position-y 50  # Position at custom coordinates
  %(prog)s --monitor 1                  # Display on secondary monitor
  %(prog)s --mode countdown --duration 10m --monitor 2  # Timer on third monitor
  %(prog)s --all-monitors               # Display on all monitors
  %(prog)s --mode midnight --all-monitors --style lightbulb  # Midnight with sci-fi glow
        """
    )

    parser.add_argument('--mode',
                        choices=['clock', 'countdown', 'midnight', 'deadline'],
                        default='clock',
                        help='Widget mode: clock (current time), countdown (timer), midnight (countdown to end of day), deadline (horror-style countdown)')

    parser.add_argument('--duration',
                        type=str,
                        help='Duration for countdown mode (e.g., 30m, 1h, 1h30m45s)')

    parser.add_argument('--color',
                        type=str,
                        help='Text color in hex format (e.g., #FF0000 for red)')

    parser.add_argument('--font-size',
                        type=int,
                        help='Font size in pixels')

    parser.add_argument('--opacity',
                        type=float,
                        help='Text opacity (0.0 to 1.0)')

    parser.add_argument('--position',
                        choices=['top', 'center', 'bottom'],
                        help='Position preset: top, center, bottom')

    parser.add_argument('--position-x',
                        type=int,
                        help='Custom X position in pixels (use with --position-y)')

    parser.add_argument('--position-y',
                        type=int,
                        help='Custom Y position in pixels (use with --position-x)')

    parser.add_argument('--monitor',
                        type=int,
                        help='Monitor index to display on (0 = primary, 1 = secondary, etc.)')

    parser.add_argument('--all-monitors',
                        action='store_true',
                        help='Display widget on all connected monitors')

    parser.add_argument('--style',
                        choices=['normal', 'lightbulb'],
                        help='Visual style: normal (default) or lightbulb (particle-based light trails inspired by 3 Body Problem)')

    args = parser.parse_args()

    # Validate monitor arguments
    if args.all_monitors and args.monitor is not None:
        parser.error("Cannot use both --monitor and --all-monitors together")

    # Validate countdown mode requirements
    if args.mode in ['countdown', 'deadline'] and not args.duration:
        parser.error("--duration is required when using countdown or deadline mode")

    # Validate position arguments
    if args.position and (args.position_x is not None or args.position_y is not None):
        parser.error("Cannot use both --position and --position-x/--position-y together")

    return args


class InTimeWidget(Gtk.Window):
    # Class variables to share IPC server across all instances
    _shared_ipc_server = None
    _all_instances = []

    def __init__(self, mode='clock', duration=None, cli_overrides=None, monitor_index=None):
        super().__init__()

        # Register this instance globally
        InTimeWidget._all_instances.append(self)

        # Connect realize signal for click-through setup
        self.connect('realize', self._on_realize)

        # Connect close signal for cleanup
        self.connect('close-request', self._on_shutdown)

        # Load config from file (merged with CLI overrides)
        self.config = self.load_config()
        if cli_overrides:
            self.config.update(cli_overrides)

        # Mode and duration settings
        self.mode = mode
        self.duration_seconds = duration
        self.end_time = None
        self.is_flashing = False
        self.flash_state = False  # For toggling flash effect
        self.monitor_index = monitor_index

        # Animation state for light bulb effect
        self.animation_frame = 0
        self.glow_intensity = 0.0
        self.flicker_offsets = []  # Random flicker for each character

        # Forbidden alarm state
        self.forbidden_alarm_active = False
        self.forbidden_alarm_message = ""
        self.forbidden_window_class = ""
        self.forbidden_window_title = ""
        self.alarm_intensity = 0.0  # 0.0 to 1.0
        self.alarm_wave_offset = 0
        self.alarm_shake_offset = (0, 0)

        # Deadline mode animation state
        self.deadline_pulse_frame = 0
        self.deadline_tick_state = False
        self.deadline_flicker_chance = 0.1  # 10% chance per second
        self.deadline_last_second = -1  # Track when seconds change for tick effect
        self.animation_timer_running = False  # Track if 50ms animation timer is active
        self.alarm_animation_timer_running = False  # Track if alarm animation timer is active
        self.alarm_animation_timer_id = None  # Store timer ID for cancellation

        # Set up signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._handle_unix_signal)
        signal.signal(signal.SIGINT, self._handle_unix_signal)

        # IPC server (shared across all instances)
        if InTimeWidget._shared_ipc_server is None:
            InTimeWidget._shared_ipc_server = IPCServer()
        self.ipc_server = InTimeWidget._shared_ipc_server

        # Calculate end time for countdown modes
        if self.mode in ['countdown', 'deadline'] and self.duration_seconds:
            self.end_time = datetime.now() + timedelta(seconds=self.duration_seconds)
        elif self.mode == 'midnight':
            # Calculate time until midnight (23:59:59)
            now = datetime.now()
            tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            self.end_time = tomorrow - timedelta(seconds=1)  # 23:59:59 today

        # Disable GTK theme at the settings level
        settings = Gtk.Settings.get_default()
        if settings:
            settings.set_property("gtk-theme-name", "")
            settings.set_property("gtk-application-prefer-dark-theme", False)

        # Set up CSS FIRST before initializing layer shell
        self.setup_css()

        # Initialize layer shell
        LayerShell.init_for_window(self)
        LayerShell.set_layer(self, LayerShell.Layer.OVERLAY)
        LayerShell.set_namespace(self, "intime-widget")

        # Set monitor if specified
        if self.monitor_index is not None:
            self._set_monitor(self.monitor_index)

        # Apply position configuration
        self._apply_position_config()

        # Set keyboard interactivity to none (click-through behavior)
        LayerShell.set_keyboard_mode(self, LayerShell.KeyboardMode.NONE)

        # Configure window properties
        self.set_decorated(False)

        # Create DrawingArea for rendering (bypasses GTK theme)
        self.drawing_area = Gtk.DrawingArea()
        self.drawing_area.set_draw_func(self.on_draw, None)

        # Add drawing area to window
        self.set_child(self.drawing_area)

        # Initialize screen color monitor
        if self.config.get('screen_sampling', {}).get('enabled', False):
            update_interval = self.config.get('screen_sampling', {}).get('update_interval', 0.5)
            throttle_threshold = self.config.get('screen_sampling', {}).get('throttle_threshold', 15)

            self.screen_color_monitor = ScreenColorMonitor(
                callback=self._on_screen_color_change,
                update_interval=update_interval,
                throttle_threshold=throttle_threshold
            )
            # Auto-start monitoring if enabled in config
            self.screen_color_monitor.start()
            print(f"ScreenColorMonitor: Started (sampling every {update_interval}s, threshold={throttle_threshold})")

        # Register IPC commands (only for first instance)
        if len(InTimeWidget._all_instances) == 1:
            self.ipc_server.register_callback('reload_config', self._handle_reload_config_command_broadcast)
            self.ipc_server.register_callback('status', self._handle_status_command)
            self.ipc_server.register_callback('forbidden_alarm', self._handle_forbidden_alarm_command_broadcast)
            self.ipc_server.register_callback('dismiss_alarm', self._handle_dismiss_alarm_command_broadcast)
            self.ipc_server.register_callback('reset_deadline', self._handle_reset_deadline_command_broadcast)
            self.ipc_server.register_callback('toggle_screen_sampling', self._handle_toggle_screen_sampling_command)
            self.ipc_server.start()

        # Update time every second
        GLib.timeout_add_seconds(1, self.update_time)

        # For lightbulb style or deadline mode, add higher frequency animation updates
        if self.config.get('style', 'normal') == 'lightbulb':
            GLib.timeout_add(50, self.update_animation)  # 20fps for smooth animation
            self.animation_timer_running = True
        elif self.mode == 'deadline':
            # Deadline mode uses slower frame rate to reduce CPU (horror effect doesn't need high fps)
            GLib.timeout_add(333, self.update_animation)  # 3fps for deadline animation
            self.animation_timer_running = True

    def _handle_unix_signal(self, signum, frame):
        """Handle UNIX signals (SIGTERM, SIGINT) for graceful shutdown"""
        print(f"Received signal {signum}, shutting down gracefully...")
        sys.exit(0)

    def _on_shutdown(self, widget):
        """Handle application shutdown"""
        print("Shutting down InTimeWidget...")

        # Remove this instance from the global list
        if self in InTimeWidget._all_instances:
            InTimeWidget._all_instances.remove(self)

        # Clean up screen color monitor if it exists
        if hasattr(self, 'screen_color_monitor'):
            try:
                self.screen_color_monitor.stop()
            except:
                pass

        return False  # Allow window to close

    def _on_realize(self, widget):
        """Set empty input region for click-through behavior"""
        # Disable can_focus to prevent any interaction
        self.set_can_focus(False)
        self.set_can_target(False)

        # Get the GDK surface and set empty input region
        surface = self.get_surface()
        if surface:
            # Create empty region - allows all mouse events to pass through
            empty_region = cairo.Region([])
            surface.set_input_region(empty_region)

            # Also try setting it again after a brief moment
            # Some compositors need the region set after full initialization
            GLib.timeout_add(100, lambda: self._set_input_region_delayed(surface))

    def _set_input_region_delayed(self, surface):
        """Set input region again after initialization (workaround for some compositors)"""
        if surface:
            empty_region = cairo.Region([])
            surface.set_input_region(empty_region)
        return False  # Don't repeat the timeout

    def _set_monitor(self, monitor_index):
        """Set the widget to display on a specific monitor"""
        display = self.get_display()
        if not display:
            print(f"Warning: Could not get display")
            return

        # Get list of monitors
        monitors = display.get_monitors()
        if not monitors:
            print(f"Warning: No monitors found")
            return

        n_monitors = monitors.get_n_items()
        print(f"Found {n_monitors} monitor(s)")

        if monitor_index < 0 or monitor_index >= n_monitors:
            print(f"Warning: Monitor index {monitor_index} out of range (0-{n_monitors-1})")
            print(f"Using default monitor")
            return

        monitor = monitors.get_item(monitor_index)
        if monitor:
            model = monitor.get_model() if hasattr(monitor, 'get_model') else "Unknown"
            print(f"Setting widget to monitor {monitor_index}: {model}")
            LayerShell.set_monitor(self, monitor)
        else:
            print(f"Warning: Could not get monitor at index {monitor_index}")

    def _apply_position_config(self):
        """Apply position configuration from config.json"""
        position_mode = self.config.get('position_mode', 'preset')

        if position_mode == 'custom':
            # Custom position mode - use specified coordinates
            # None means "center along this axis"
            position_x = self.config.get('position_x', None)
            position_y = self.config.get('position_y', None)

            # Handle X axis positioning
            if position_x is not None:
                # Anchor to left edge with margin for X offset
                LayerShell.set_anchor(self, LayerShell.Edge.LEFT, True)
                LayerShell.set_anchor(self, LayerShell.Edge.RIGHT, False)
                LayerShell.set_margin(self, LayerShell.Edge.LEFT, position_x)
            else:
                # Center horizontally by anchoring to both left and right
                LayerShell.set_anchor(self, LayerShell.Edge.LEFT, True)
                LayerShell.set_anchor(self, LayerShell.Edge.RIGHT, True)

            # Handle Y axis positioning
            if position_y is not None:
                # Anchor to top edge with margin for Y offset
                LayerShell.set_anchor(self, LayerShell.Edge.TOP, True)
                LayerShell.set_anchor(self, LayerShell.Edge.BOTTOM, False)
                LayerShell.set_margin(self, LayerShell.Edge.TOP, position_y)
            else:
                # Center vertically by anchoring to both top and bottom
                LayerShell.set_anchor(self, LayerShell.Edge.TOP, True)
                LayerShell.set_anchor(self, LayerShell.Edge.BOTTOM, True)

            print(f"Custom position mode: x={position_x}, y={position_y}")

        else:
            # Preset position mode
            position_preset = self.config.get('position_preset', 'center')

            if position_preset == 'center':
                # Full-screen for centered clock
                LayerShell.set_anchor(self, LayerShell.Edge.TOP, True)
                LayerShell.set_anchor(self, LayerShell.Edge.BOTTOM, True)
                LayerShell.set_anchor(self, LayerShell.Edge.LEFT, True)
                LayerShell.set_anchor(self, LayerShell.Edge.RIGHT, True)

            elif position_preset == 'top':
                # Top of screen, full width
                LayerShell.set_anchor(self, LayerShell.Edge.TOP, True)
                LayerShell.set_anchor(self, LayerShell.Edge.LEFT, True)
                LayerShell.set_anchor(self, LayerShell.Edge.RIGHT, True)
                LayerShell.set_anchor(self, LayerShell.Edge.BOTTOM, False)

            elif position_preset == 'bottom':
                # Bottom of screen, full width
                LayerShell.set_anchor(self, LayerShell.Edge.BOTTOM, True)
                LayerShell.set_anchor(self, LayerShell.Edge.LEFT, True)
                LayerShell.set_anchor(self, LayerShell.Edge.RIGHT, True)
                LayerShell.set_anchor(self, LayerShell.Edge.TOP, False)

    def on_draw(self, area, cr, width, height, user_data):
        """Custom draw function using Cairo - bypasses GTK theme completely"""
        # Clear the background with transparency
        cr.save()
        cr.set_source_rgba(0, 0, 0, 0)
        cr.set_operator(cairo.Operator.SOURCE)
        cr.paint()
        cr.restore()

        # Get display string based on mode
        now = datetime.now()
        time_str = ""

        if self.mode == 'clock':
            # Standard clock mode
            time_str = now.strftime("%H:%M:%S")
        elif self.mode in ['countdown', 'midnight', 'deadline']:
            # Calculate remaining time
            if self.end_time:
                remaining = self.end_time - now

                if remaining.total_seconds() <= 0:
                    # Countdown finished
                    if self.mode == 'deadline':
                        # Trigger forbidden alarm for deadline mode
                        if not self.forbidden_alarm_active:
                            self.forbidden_alarm_active = True
                            self.forbidden_alarm_message = "DEADLINE REACHED"
                            # Start alarm animation timer if not already running
                            if not self.alarm_animation_timer_running:
                                # Reduced from 50ms to 100ms (10fps instead of 20fps) for better performance
                                self.alarm_animation_timer_id = GLib.timeout_add(100, self.update_alarm_animation)
                                self.alarm_animation_timer_running = True
                    else:
                        # Regular flashing for countdown/midnight
                        self.is_flashing = True
                    time_str = "00:00:00"
                else:
                    # Format remaining time
                    total_seconds = int(remaining.total_seconds())
                    hours = total_seconds // 3600
                    minutes = (total_seconds % 3600) // 60
                    seconds = total_seconds % 60

                    # Use HH:MM:SS format for countdown (decremental clock)
                    time_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            else:
                time_str = "ERROR"

        # Parse color from config
        color = self.config.get('color', '#00FF00')

        # If flashing, toggle between configured color and red
        if self.is_flashing:
            if self.flash_state:
                color = '#FF0000'  # Red for alert
            # else: use configured color
            # Flash state will be toggled in update_time()

        # Convert hex color to RGB (for idle text and normal rendering)
        r = int(color[1:3], 16) / 255.0
        g = int(color[3:5], 16) / 255.0
        b = int(color[5:7], 16) / 255.0
        opacity = self.config.get('opacity', 0.5)

        # Set up Pango layout for text
        layout = PangoCairo.create_layout(cr)
        font_size_px = self.config.get('font_size', 100)

        # Use thin font for lightbulb style (like thin wire filaments)
        style = self.config.get('style', 'normal')
        if style == 'lightbulb':
            # Use ultra-light weight for thin wire effect
            font_desc = Pango.FontDescription("monospace ultra-light")
        else:
            font_desc = Pango.FontDescription("monospace bold")

        font_desc.set_absolute_size(font_size_px * Pango.SCALE)
        layout.set_font_description(font_desc)
        layout.set_text(time_str, -1)

        # Get text dimensions for centering
        text_width, text_height = layout.get_pixel_size()

        # Center the text in the available space
        x = max(0, (width - text_width) / 2)
        y = max(0, (height - text_height) / 2)

        # Get style for rendering decisions
        style = self.config.get('style', 'normal')

        # Check if forbidden alarm is active (overrides all other styles)
        if self.forbidden_alarm_active and self.alarm_intensity > 0.05:
            # Draw intense forbidden alarm
            self._draw_forbidden_alarm(cr, layout, x, y, time_str, width, height)
        elif self.mode == 'deadline' and style != 'normal' and not self.forbidden_alarm_active:
            # Draw deadline mode countdown with horror effects (using dynamic color)
            # Only use horror effects if style is NOT explicitly set to 'normal'
            self._draw_deadline_countdown(cr, layout, x, y, time_str, width, height, r, g, b)
        else:
            # Normal rendering path (used for clock, countdown, midnight, and deadline with --style normal)

            if style == 'lightbulb':
                # Light bulb string effect with glow and animation
                self._draw_lightbulb_text(cr, layout, x, y, time_str)
            elif style == 'bordered':
                # Bordered text with thin dark outline
                self._draw_bordered_text(cr, layout, x, y, r, g, b, opacity)
            else:
                # Normal rendering
                # Draw the text
                cr.move_to(x, y)
                cr.set_source_rgba(r, g, b, opacity)
                PangoCairo.show_layout(cr, layout)

    def _draw_lightbulb_text(self, cr, layout, x, y, time_str):
        """Draw text with 3 Body Problem countdown effect - thin glowing line trails"""
        # Calculate animated glow intensity
        base_pulse = 0.85 + 0.15 * math.sin(self.animation_frame * 0.08)
        shimmer = 1.0 + (random.random() - 0.5) * 0.1
        glow_intensity = base_pulse * shimmer

        # Draw many overlapping thin strokes with slight offsets for cloudy effect
        # Reduced from 30 to 15 for better performance
        num_layers = 15  # Thin lines create the cloudy light trail

        for i in range(num_layers):
            # Random offset for each layer (creates the shallow cloudy spread)
            offset_x = random.uniform(-1.2, 1.2)
            offset_y = random.uniform(-1.2, 1.2)

            # Get the text outline path
            cr.new_path()
            cr.move_to(x + offset_x, y + offset_y)
            PangoCairo.layout_path(cr, layout)

            # Very thin line width
            line_width = random.uniform(0.3, 0.8)
            cr.set_line_width(line_width)

            # Random opacity for depth/cloudy effect
            opacity = random.uniform(0.08, 0.15) * glow_intensity

            # Blue-white glow color with variation
            r = random.uniform(0.85, 0.98)
            g = random.uniform(0.92, 1.0)
            b = 1.0

            cr.set_source_rgba(r, g, b, opacity)
            cr.stroke()

        # Add a few brighter core lines for definition
        # Reduced from 8 to 4 for better performance
        for i in range(4):
            offset_x = random.uniform(-0.5, 0.5)
            offset_y = random.uniform(-0.5, 0.5)

            cr.new_path()
            cr.move_to(x + offset_x, y + offset_y)
            PangoCairo.layout_path(cr, layout)

            cr.set_line_width(random.uniform(0.4, 0.7))
            cr.set_source_rgba(0.95, 0.98, 1.0, random.uniform(0.2, 0.3) * glow_intensity)
            cr.stroke()

    def _draw_bordered_text(self, cr, layout, x, y, r, g, b, opacity):
        """Draw text with a thin dark border/outline"""
        # Draw a thin border stroke
        cr.move_to(x, y)
        PangoCairo.layout_path(cr, layout)
        cr.set_line_width(1.8)
        # Use fully opaque black for the border
        cr.set_source_rgba(0.0, 0.0, 0.0, 1.0)
        cr.stroke()

        # Then fill the text with the configured color
        cr.move_to(x, y)
        PangoCairo.layout_path(cr, layout)
        cr.set_source_rgba(r, g, b, opacity)
        cr.fill()

    def _draw_forbidden_alarm(self, cr, layout, x, y, time_str, width, height):
        """Draw intense forbidden alarm with all effects combined"""
        import math

        # Apply shake offset to main position
        shake_x = x + self.alarm_shake_offset[0]
        shake_y = y + self.alarm_shake_offset[1]

        # 1. Draw expanding red waves/circles from center
        center_x = width / 2
        center_y = height / 2

        for i in range(3):
            wave_radius = (self.alarm_wave_offset + i * 60) % 200 + 50
            wave_alpha = (1.0 - wave_radius / 250) * self.alarm_intensity * 0.3

            cr.arc(center_x, center_y, wave_radius, 0, 2 * math.pi)
            cr.set_source_rgba(1.0, 0.0, 0.0, wave_alpha)
            cr.set_line_width(3)
            cr.stroke()

        # 2. Draw pulsing red glow around text (many layers)
        pulse = 0.7 + 0.3 * math.sin(self.animation_frame * 0.2)
        glow_intensity = pulse * self.alarm_intensity

        num_layers = 12  # Intense glow (reduced from 40 for performance)
        for i in range(num_layers):
            offset_x = random.uniform(-3, 3)
            offset_y = random.uniform(-3, 3)

            cr.new_path()
            cr.move_to(shake_x + offset_x, shake_y + offset_y)
            PangoCairo.layout_path(cr, layout)

            line_width = random.uniform(0.5, 1.5)
            cr.set_line_width(line_width)

            # Increased opacity since we have fewer layers
            opacity = random.uniform(0.2, 0.5) * glow_intensity
            r = 1.0
            g = random.uniform(0.0, 0.1)  # Mostly red
            b = 0.0

            cr.set_source_rgba(r, g, b, opacity)
            cr.stroke()

        # 3. Draw main text (bright red, high opacity)
        cr.new_path()
        cr.move_to(shake_x, shake_y)
        PangoCairo.layout_path(cr, layout)

        # Very bright core
        cr.set_source_rgba(1.0, 0.0, 0.0, 0.95 * self.alarm_intensity)
        cr.fill()

        # 4. Add white hot center for intensity
        # Reduced from 5 to 3 for performance
        for i in range(3):
            offset_x = random.uniform(-0.5, 0.5)
            offset_y = random.uniform(-0.5, 0.5)

            cr.new_path()
            cr.move_to(shake_x + offset_x, shake_y + offset_y)
            PangoCairo.layout_path(cr, layout)

            cr.set_source_rgba(1.0, 1.0, 1.0, random.uniform(0.3, 0.5) * glow_intensity)
            cr.set_line_width(0.3)
            cr.stroke()

        # 5. Draw custom message below clock
        if self.forbidden_alarm_message and self.alarm_intensity > 0.5:
            message_layout = PangoCairo.create_layout(cr)
            message_font = Pango.FontDescription("sans bold")
            message_font.set_absolute_size(24 * Pango.SCALE)
            message_layout.set_font_description(message_font)
            message_layout.set_text(self.forbidden_alarm_message, -1)

            msg_width, msg_height = message_layout.get_pixel_size()
            msg_x = (width - msg_width) / 2
            msg_y = shake_y + layout.get_pixel_size()[1] + 30

            # Pulsing message
            msg_pulse = 0.8 + 0.2 * math.sin(self.animation_frame * 0.15)

            # Draw glow (reduced from 10 to 5 for performance)
            for i in range(5):
                offset_x = random.uniform(-2, 2)
                offset_y = random.uniform(-2, 2)
                cr.move_to(msg_x + offset_x, msg_y + offset_y)
                cr.set_source_rgba(1.0, 0.0, 0.0, random.uniform(0.1, 0.2) * msg_pulse)
                PangoCairo.show_layout(cr, message_layout)

            # Draw main message
            cr.move_to(msg_x, msg_y)
            cr.set_source_rgba(1.0, 0.0, 0.0, 0.95 * msg_pulse)
            PangoCairo.show_layout(cr, message_layout)

    def _draw_deadline_countdown(self, cr, layout, x, y, time_str, width, height, base_r=1.0, base_g=0.0, base_b=0.0):
        """Draw horror movie-style deadline countdown with ticking animation

        Args:
            base_r, base_g, base_b: Base color (from config or screen sampling)
        """
        import math

        # Get current remaining seconds to determine urgency level
        remaining_seconds = 0
        if self.end_time:
            remaining = self.end_time - datetime.now()
            remaining_seconds = max(0, remaining.total_seconds())

        # Urgency increases as time runs out
        urgency = 1.0
        if remaining_seconds > 300:  # More than 5 minutes
            urgency = 0.5
        elif remaining_seconds > 60:  # More than 1 minute
            urgency = 0.7
        elif remaining_seconds > 10:  # More than 10 seconds
            urgency = 0.9
        else:  # Final 10 seconds
            urgency = 1.0

        # Base opacity (low to not obstruct screen, but visible enough for deadline mode)
        # Use higher default opacity for deadline mode to ensure visibility
        base_opacity = self.config.get('opacity', 0.35)

        # Pulsing effect - slower pulse for less urgency
        pulse_speed = 0.08 * (1.0 + urgency)
        pulse = 0.7 + 0.3 * math.sin(self.deadline_pulse_frame * pulse_speed)

        # Tick effect - creates a brief flash when seconds change
        tick_intensity = 0.0
        current_second = int(remaining_seconds) % 60
        if current_second != self.deadline_last_second:
            self.deadline_last_second = current_second
            self.deadline_tick_state = True

        if self.deadline_tick_state:
            # Brief flash effect on tick
            tick_intensity = 0.3 * urgency
            # Reset tick state after brief moment (handled in animation update)

        # Random flicker effect (horror movie aesthetic)
        flicker = 1.0
        if random.random() < self.deadline_flicker_chance * urgency:
            flicker = random.uniform(0.7, 1.0)

        # Color scheme - use dynamic color with urgency-based intensity
        # Dims color at low urgency, brightens as deadline approaches
        if urgency < 0.7:
            # Darker intensity for low urgency
            r, g, b = base_r * 0.55, base_g * 0.55, base_b * 0.55
        elif urgency < 0.9:
            # Medium intensity
            r, g, b = base_r * 0.8, base_g * 0.8, base_b * 0.8
        else:
            # Full brightness at high urgency
            r, g, b = base_r, base_g, base_b

        # 2. Draw dark red glow around text (layered for depth)
        # Reduced layer count for better performance
        num_glow_layers = max(2, int(4 * urgency))  # Max 4 layers for low CPU usage
        for i in range(num_glow_layers):
            offset_x = random.uniform(-2.5, 2.5)
            offset_y = random.uniform(-2.5, 2.5)

            cr.new_path()
            cr.move_to(x + offset_x, y + offset_y)
            PangoCairo.layout_path(cr, layout)

            line_width = random.uniform(0.4, 1.0)
            cr.set_line_width(line_width)

            # Glow should be independent of base_opacity for better visibility
            # Increased opacity per layer since we have fewer layers
            glow_opacity = random.uniform(0.3, 0.6) * pulse * flicker * urgency
            cr.set_source_rgba(r, g * 0.2, b, glow_opacity)
            cr.stroke()

        # 3. Draw main countdown text
        cr.new_path()
        cr.move_to(x, y)
        PangoCairo.layout_path(cr, layout)

        # Main text opacity influenced by pulse, tick, and flicker
        main_opacity = base_opacity * pulse * flicker * (1.0 + tick_intensity)
        cr.set_source_rgba(r, g, b, main_opacity)
        cr.fill()

        # 4. Add extra bright center strokes for intensity (especially on tick)
        if urgency > 0.5 or tick_intensity > 0:
            num_center_layers = int(5 * urgency) + (3 if tick_intensity > 0 else 0)
            for i in range(num_center_layers):
                offset_x = random.uniform(-0.8, 0.8)
                offset_y = random.uniform(-0.8, 0.8)

                cr.new_path()
                cr.move_to(x + offset_x, y + offset_y)
                PangoCairo.layout_path(cr, layout)

                # Bright strokes should be more visible, independent of base_opacity
                bright_opacity = random.uniform(0.15, 0.35) * pulse * urgency
                if tick_intensity > 0:
                    bright_opacity *= 1.8  # Extra bright on tick

                # Mix in some orange for a fiery look at high urgency
                bright_r = r
                bright_g = g + (0.3 * urgency) if urgency > 0.8 else g

                cr.set_source_rgba(bright_r, bright_g, b, bright_opacity)
                cr.set_line_width(random.uniform(0.3, 0.7))
                cr.stroke()

    def load_config(self):
        """Load configuration from config.json"""
        # Try user config directory first
        user_config_path = os.path.expanduser("~/.config/intime/config.json")
        # Fall back to example config in repo
        repo_config_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'config.example.json')

        default_config = {
            "color": "#FFFFFF",
            "font_size": 78,
            "style": "normal",
            "opacity": 0.5,
            "position_mode": "preset",
            "position_preset": "center"
        }

        # Try user config first
        try:
            if os.path.exists(user_config_path):
                with open(user_config_path, 'r') as f:
                    config = json.load(f)
                    print(f"Config loaded from: {user_config_path}")
                    # Merge with defaults to handle missing keys
                    return {**default_config, **config}
        except Exception as e:
            print(f"Error loading user config: {e}")

        # Try repo example config
        try:
            if os.path.exists(repo_config_path):
                with open(repo_config_path, 'r') as f:
                    config = json.load(f)
                    print(f"Config loaded from: {repo_config_path}")
                    # Merge with defaults to handle missing keys
                    return {**default_config, **config}
        except Exception as e:
            print(f"Error loading repo config: {e}")

        print("Using default configuration")
        return default_config

    def setup_css(self):
        """Set up CSS for styling and transparency"""
        css_provider = Gtk.CssProvider()

        # CSS with configurable color and transparency
        color = self.config.get('color', '#FFFFFF')
        font_size = self.config.get('font_size', 48)

        css = """
        * {
            background-color: rgba(0, 0, 0, 0);
            background-image: none;
            background: none;
        }

        window,
        window.background {
            background-color: rgba(0, 0, 0, 0);
            background-image: none;
            background: none;
            border: none;
            box-shadow: none;
        }

        drawingarea {
            background-color: rgba(0, 0, 0, 0);
            background-image: none;
            background: none;
        }
        """

        css_provider.load_from_string(css)

        # Apply CSS with maximum priority to override all themes
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_USER
        )

    # ===== IPC Command Handlers =====

    def _handle_reload_config_command(self, args):
        """Handle reload_config IPC command - reload configuration from config.json"""
        try:
            # Reload config from file
            self.config = self.load_config()

            # Update CSS with new config
            GLib.idle_add(self.setup_css)

            # Force redraw
            GLib.idle_add(self.drawing_area.queue_draw)

            return json.dumps({"status": "success", "message": "Config reloaded. Restart overlay to apply position changes."})
        except Exception as e:
            return json.dumps({"status": "error", "message": str(e)})

    def _on_screen_color_change(self, sampled_hex_color):
        """
        Handle screen color change event from real-time screen sampling.
        Applies hybrid complementary + contrast processing.
        """
        try:
            # Get background color from config (default to black)
            bg_color = self.config.get('background_color', '#000000')

            # Process color using hybrid approach (complementary + contrast)
            final_color = HybridColorProcessor.process_color(
                sampled_hex=sampled_hex_color,
                background_hex=bg_color,
                min_contrast_ratio=3.0
            )

            print(f"ScreenColorMonitor: Sampled {sampled_hex_color} -> Processed {final_color}")

            # Update config
            self.config['color'] = final_color

            # Update CSS
            GLib.idle_add(self.setup_css)

            # Force redraw
            GLib.idle_add(self.drawing_area.queue_draw)

        except Exception as e:
            print(f"ScreenColorMonitor: Error updating color: {e}")
            import traceback
            traceback.print_exc()

    def _handle_toggle_screen_sampling_command(self, args):
        """Handle toggle_screen_sampling IPC command - enable/disable real-time screen sampling"""
        try:
            if not hasattr(self, 'screen_color_monitor'):
                return json.dumps({"success": False, "message": "Screen color monitor not initialized"})

            # Toggle the monitor
            new_state = self.screen_color_monitor.toggle()

            return json.dumps({
                "success": True,
                "enabled": new_state,
                "message": f"Screen sampling {'enabled' if new_state else 'disabled'}"
            })
        except Exception as e:
            return json.dumps({"success": False, "message": str(e)})

    def _handle_status_command(self, args):
        """Handle status IPC command - return current overlay status"""
        try:
            return json.dumps({
                "status": "running",
                "mode": self.mode,
                "config": {
                    "color": self.config.get('color'),
                    "font_size": self.config.get('font_size'),
                    "opacity": self.config.get('opacity'),
                    "style": self.config.get('style'),
                    "position_mode": self.config.get('position_mode'),
                    "position_preset": self.config.get('position_preset')
                }
            })
        except Exception as e:
            return json.dumps({"status": "error", "message": str(e)})

    def _handle_forbidden_alarm_command(self, args):
        """Handle forbidden_alarm IPC command - trigger intense alarm visual"""
        try:
            # Parse args: "window_class|window_title|message"
            if args:
                parts = args.split('|', 2)
                self.forbidden_window_class = parts[0] if len(parts) > 0 else ""
                self.forbidden_window_title = parts[1] if len(parts) > 1 else ""
                self.forbidden_alarm_message = parts[2] if len(parts) > 2 else "This window is forbidden!"
            else:
                self.forbidden_alarm_message = "This window is forbidden!"

            # Activate alarm
            self.forbidden_alarm_active = True
            self.alarm_intensity = 0.0  # Will ramp up in animation

            # Start alarm animation timer if not already running
            if not self.alarm_animation_timer_running:
                # Reduced from 50ms to 100ms (10fps instead of 20fps) for better performance
                self.alarm_animation_timer_id = GLib.timeout_add(100, self.update_alarm_animation)
                self.alarm_animation_timer_running = True

            print(f"Forbidden alarm activated: {self.forbidden_alarm_message}")

            return json.dumps({"success": True, "message": "Alarm activated"})
        except Exception as e:
            return json.dumps({"success": False, "message": str(e)})

    def _handle_dismiss_alarm_command(self, args):
        """Handle dismiss_alarm IPC command - turn off alarm"""
        try:
            self.forbidden_alarm_active = False
            self.alarm_intensity = 0.0
            self.forbidden_alarm_message = ""

            print("Forbidden alarm dismissed")

            return json.dumps({"success": True, "message": "Alarm dismissed"})
        except Exception as e:
            return json.dumps({"success": False, "message": str(e)})

    def _handle_reset_deadline_command(self, args):
        """Handle reset_deadline IPC command - reset from deadline mode back to clock mode"""
        try:
            # Only reset if we're in deadline mode or alarm is active
            if self.mode == 'deadline' or self.forbidden_alarm_active:
                # Reset to clock mode
                self.mode = 'clock'
                self.duration_seconds = None
                self.end_time = None
                self.is_flashing = False

                # Dismiss any active alarms
                self.forbidden_alarm_active = False
                self.alarm_intensity = 0.0
                self.forbidden_alarm_message = ""

                # Reset deadline animation state
                self.deadline_pulse_frame = 0
                self.deadline_tick_state = False
                self.deadline_last_second = -1

                # Force redraw
                GLib.idle_add(self.drawing_area.queue_draw)

                print("Deadline mode reset to normal clock")
                return json.dumps({"success": True, "message": "Reset to clock mode"})
            else:
                return json.dumps({"success": False, "message": "Not in deadline mode"})
        except Exception as e:
            return json.dumps({"success": False, "message": str(e)})

    # ===== Broadcast IPC Handlers (for multi-monitor support) =====

    def _handle_reload_config_command_broadcast(self, args):
        """Broadcast reload_config to all widget instances"""
        result = None
        for instance in InTimeWidget._all_instances:
            result = instance._handle_reload_config_command(args)
        return result if result else json.dumps({"status": "error", "message": "No instances available"})

    def _handle_forbidden_alarm_command_broadcast(self, args):
        """Broadcast forbidden_alarm to all widget instances"""
        result = None
        for instance in InTimeWidget._all_instances:
            result = instance._handle_forbidden_alarm_command(args)
        return result if result else json.dumps({"success": False, "message": "No instances available"})

    def _handle_dismiss_alarm_command_broadcast(self, args):
        """Broadcast dismiss_alarm to all widget instances"""
        result = None
        for instance in InTimeWidget._all_instances:
            result = instance._handle_dismiss_alarm_command(args)
        return result if result else json.dumps({"success": False, "message": "No instances available"})

    def _handle_reset_deadline_command_broadcast(self, args):
        """Broadcast reset_deadline to all widget instances"""
        result = None
        for instance in InTimeWidget._all_instances:
            result = instance._handle_reset_deadline_command(args)
        return result if result else json.dumps({"success": False, "message": "No instances available"})

    # ===== Animation and Update Functions =====

    def update_time(self):
        """Update the clock by triggering a redraw"""
        # Toggle flash state if flashing
        if self.is_flashing:
            self.flash_state = not self.flash_state

        self.drawing_area.queue_draw()
        return True  # Continue the timeout

    def update_animation(self):
        """Update animation frame for light bulb and deadline effects"""
        self.animation_frame += 1

        # Update deadline mode animation
        if self.mode == 'deadline':
            self.deadline_pulse_frame += 1
            # Reset tick state after brief moment (about 100ms / 2 frames at 50ms interval)
            if self.deadline_tick_state and self.deadline_pulse_frame % 2 == 0:
                self.deadline_tick_state = False

        self.drawing_area.queue_draw()
        return True  # Continue the timeout

    def update_alarm_animation(self):
        """Update alarm animation effects (pulsing, shaking, waves)"""
        if self.forbidden_alarm_active:
            # Ramp up intensity quickly
            if self.alarm_intensity < 1.0:
                self.alarm_intensity = min(1.0, self.alarm_intensity + 0.1)

            # Update wave offset for expanding circles
            self.alarm_wave_offset = (self.alarm_wave_offset + 5) % 200

            # Update shake offset for jitter effect
            import random
            shake_magnitude = 3 * self.alarm_intensity  # Shake more as intensity increases
            self.alarm_shake_offset = (
                random.randint(-int(shake_magnitude), int(shake_magnitude)),
                random.randint(-int(shake_magnitude), int(shake_magnitude))
            )

            # Force redraw
            self.drawing_area.queue_draw()
            return True  # Continue the timeout
        else:
            # Ramp down intensity when dismissed
            if self.alarm_intensity > 0.0:
                self.alarm_intensity = max(0.0, self.alarm_intensity - 0.15)
                self.drawing_area.queue_draw()
                return True  # Continue animating the fade-out
            else:
                # Alarm is fully dismissed and faded out - stop the timer
                self.alarm_animation_timer_running = False
                self.alarm_animation_timer_id = None
                return False  # Stop the timeout


class InTimeApplication(Gtk.Application):
    def __init__(self, mode='clock', duration=None, cli_overrides=None, monitor_index=None, all_monitors=False):
        super().__init__(application_id='com.hyprland.intimewidget')
        self.mode = mode
        self.duration = duration
        self.cli_overrides = cli_overrides
        self.monitor_index = monitor_index
        self.all_monitors = all_monitors

    def do_activate(self):
        """Called when the application is activated"""
        if not hasattr(self, 'windows_created'):
            self.windows_created = True

            if self.all_monitors:
                # Create widgets on all monitors
                self._create_widgets_on_all_monitors()
            else:
                # Create single widget
                window = InTimeWidget(
                    mode=self.mode,
                    duration=self.duration,
                    cli_overrides=self.cli_overrides,
                    monitor_index=self.monitor_index
                )
                self.add_window(window)
                window.present()

    def _create_widgets_on_all_monitors(self):
        """Create a widget on each connected monitor"""
        # Create a temporary window to get display
        temp_window = Gtk.Window()
        display = temp_window.get_display()

        if not display:
            print("Warning: Could not get display")
            return

        monitors = display.get_monitors()
        if not monitors:
            print("Warning: No monitors found")
            return

        n_monitors = monitors.get_n_items()
        print(f"Creating widgets on {n_monitors} monitor(s)")

        # Create a widget for each monitor
        for i in range(n_monitors):
            window = InTimeWidget(
                mode=self.mode,
                duration=self.duration,
                cli_overrides=self.cli_overrides,
                monitor_index=i
            )
            self.add_window(window)
            window.present()

        temp_window.destroy()


def main():
    """Main entry point"""
    # Parse command line arguments
    args = parse_args()

    # Parse duration if in countdown or deadline mode
    duration_seconds = None
    if args.mode in ['countdown', 'deadline']:
        try:
            duration_seconds = parse_duration(args.duration)
        except ValueError as e:
            print(f"Error: {e}")
            return 1

    # Build CLI overrides dictionary
    cli_overrides = {}
    if args.color:
        cli_overrides['color'] = args.color
        # Disable screen sampling when a fixed color is specified
        cli_overrides['screen_sampling'] = {'enabled': False}
    if args.font_size:
        cli_overrides['font_size'] = args.font_size
    if args.opacity is not None:
        cli_overrides['opacity'] = args.opacity
    if args.style:
        cli_overrides['style'] = args.style
    if args.position:
        cli_overrides['position_mode'] = 'preset'
        cli_overrides['position_preset'] = args.position
    if args.position_x is not None or args.position_y is not None:
        cli_overrides['position_mode'] = 'custom'
        if args.position_x is not None:
            cli_overrides['position_x'] = args.position_x
        if args.position_y is not None:
            cli_overrides['position_y'] = args.position_y

    # Create and run the application
    app = InTimeApplication(
        mode=args.mode,
        duration=duration_seconds,
        cli_overrides=cli_overrides if cli_overrides else None,
        monitor_index=args.monitor,
        all_monitors=args.all_monitors
    )
    return app.run(None)


if __name__ == '__main__':
    import sys
    sys.exit(main())
