"""
bsl_gui.py  —  wxPython GUI for Tricore BSL (Pi 5)

Usage:
    python bsl_gui.py

Requires:
    pip install wxPython
    (plus all dependencies from the original bsl.py: python-can, udsoncan,
     lgpio, lz4, tqdm, etc.)

Place this file alongside bsl.py (or adjust the import below).
All blocking operations run in background threads so the GUI stays responsive.
"""

import threading
import queue
import sys
import subprocess
import wx
import wx.lib.scrolledpanel as scrolled

# ---------------------------------------------------------------------------
# CAN bus configuration
# ---------------------------------------------------------------------------
CAN_INTERFACE = "can0"
CAN_BITRATE   = 500000
CAN_TXQUEUELEN = 65536


def bring_up_can(interface=CAN_INTERFACE, bitrate=CAN_BITRATE,
                 txqueuelen=CAN_TXQUEUELEN):
    """
    Bring up the CAN interface.  Runs:
        sudo ip link set <iface> up type can bitrate <bitrate>
        sudo ifconfig <iface> txqueuelen <txqueuelen>

    Returns a list of (cmd_string, returncode, stderr) tuples.
    """
    cmds = [
        ["sudo", "ip", "link", "set", interface,
         "up", "type", "can", "bitrate", str(bitrate)],
        ["sudo", "ifconfig", interface, "txqueuelen", str(txqueuelen)],
    ]
    results = []
    for cmd in cmds:
        p = subprocess.run(cmd, capture_output=True, text=True)
        results.append((" ".join(cmd), p.returncode, p.stderr.strip()))
    return results


def can_is_up(interface=CAN_INTERFACE):
    """Return True if the interface exists and is UP."""
    try:
        p = subprocess.run(
            ["ip", "link", "show", interface],
            capture_output=True, text=True
        )
        return p.returncode == 0 and "UP" in p.stdout
    except Exception:
        return False

# ---------------------------------------------------------------------------
# Lazy import of your existing module so the GUI opens even when hardware
# (CAN bus, GPIO) is unavailable.  Wrap in try/except for dev machines.
# ---------------------------------------------------------------------------
try:
    import bootloader as bsl
    BSL_AVAILABLE = True
except Exception as e:
    BSL_AVAILABLE = False
    BSL_IMPORT_ERROR = str(e)


# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------
CLR_SUCCESS = wx.Colour(29, 158, 117)   # teal green
CLR_FAILURE = wx.Colour(216, 90,  48)   # coral red
CLR_INFO    = wx.Colour(55, 138, 221)   # blue
CLR_MUTED   = wx.Colour(136, 135, 128)  # gray


# ---------------------------------------------------------------------------
# LogRedirect  — captures stdout/stderr and feeds them to the GUI log
# ---------------------------------------------------------------------------
class LogRedirect:
    def __init__(self, log_queue: queue.Queue):
        self._q = log_queue

    def write(self, text):
        if text.strip():
            self._q.put(text)

    def flush(self):
        pass


# ---------------------------------------------------------------------------
# Worker  — runs BSL calls in a background thread
# ---------------------------------------------------------------------------
class Worker(threading.Thread):
    def __init__(self, log_queue: queue.Queue, fn, *args, **kwargs):
        super().__init__(daemon=True)
        self._q   = log_queue
        self._fn  = fn
        self._args   = args
        self._kwargs = kwargs

    def run(self):
        try:
            result = self._fn(*self._args, **self._kwargs)
            if result is not None:
                self._q.put(f"→ Result: {result}")
        except Exception as exc:
            self._q.put(f"ERROR: {exc}")


# ---------------------------------------------------------------------------
# InputDialog  — small dialog that prompts for one or more hex fields
# ---------------------------------------------------------------------------
class InputDialog(wx.Dialog):
    def __init__(self, parent, title, fields):
        super().__init__(parent, title=title,
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.fields  = fields
        self.entries = {}

        sizer = wx.BoxSizer(wx.VERTICAL)
        grid  = wx.FlexGridSizer(cols=2, vgap=8, hgap=12)
        grid.AddGrowableCol(1)

        for label, placeholder in fields:
            grid.Add(wx.StaticText(self, label=label),
                     flag=wx.ALIGN_CENTER_VERTICAL)
            ctrl = wx.TextCtrl(self, value="", size=(220, -1))
            ctrl.SetHint(placeholder)
            grid.Add(ctrl, flag=wx.EXPAND)
            self.entries[label] = ctrl

        sizer.Add(grid, proportion=1, flag=wx.EXPAND | wx.ALL, border=16)

        btn_sizer = wx.StdDialogButtonSizer()
        ok_btn  = wx.Button(self, wx.ID_OK,     label="Run")
        cxl_btn = wx.Button(self, wx.ID_CANCEL, label="Cancel")
        ok_btn.SetDefault()
        btn_sizer.AddButton(ok_btn)
        btn_sizer.AddButton(cxl_btn)
        btn_sizer.Realize()
        sizer.Add(btn_sizer, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM,
                  border=16)

        self.SetSizerAndFit(sizer)
        self.CentreOnParent()

    def get_values(self):
        return {label: ctrl.GetValue().strip()
                for label, ctrl in self.entries.items()}


# ---------------------------------------------------------------------------
# MainFrame
# ---------------------------------------------------------------------------
class MainFrame(wx.Frame):
    TITLE = "Tricore BSL — Pi 5"

    def __init__(self):
        super().__init__(None, title=self.TITLE, size=(960, 680),
                         style=wx.DEFAULT_FRAME_STYLE)

        self._log_queue: queue.Queue = queue.Queue()
        self._busy = False

        # Redirect stdout/stderr
        redir = LogRedirect(self._log_queue)
        sys.stdout = redir
        sys.stderr = redir

        self._build_ui()
        self._start_log_timer()

        if not BSL_AVAILABLE:
            self._log(f"[WARN] bsl.py could not be imported: {BSL_IMPORT_ERROR}",
                      CLR_FAILURE)
            self._log("[WARN] GUI is in demo mode — buttons will show dialogs only.",
                      CLR_MUTED)
        else:
            self._log("BSL module loaded successfully.", CLR_SUCCESS)

        self.SetMinSize((720, 500))
        self.Centre()
        self.Show()

        # Auto bring up CAN on startup
        wx.CallAfter(self._bring_up_can_async)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        panel = wx.Panel(self)
        outer = wx.BoxSizer(wx.VERTICAL)

        # ── toolbar row ────────────────────────────────────────────────
        tb = wx.BoxSizer(wx.HORIZONTAL)

        # BSL status
        self._status_dot  = wx.StaticText(panel, label="●")
        self._status_text = wx.StaticText(panel, label="Initialising…")
        self._status_dot.SetForegroundColour(CLR_MUTED)

        # CAN status
        self._can_dot  = wx.StaticText(panel, label="●")
        self._can_text = wx.StaticText(panel, label="CAN: checking…")
        self._can_dot.SetForegroundColour(CLR_MUTED)

        can_btn   = wx.Button(panel, label="Bring up CAN", size=(-1, 28))
        clear_btn = wx.Button(panel, label="Clear log",    size=(-1, 28))
        can_btn.Bind(wx.EVT_BUTTON,   self._on_bring_up_can)
        clear_btn.Bind(wx.EVT_BUTTON, self._on_clear)

        tb.Add(self._status_dot,  flag=wx.ALIGN_CENTER_VERTICAL | wx.LEFT,  border=8)
        tb.Add(self._status_text, flag=wx.ALIGN_CENTER_VERTICAL | wx.LEFT,  border=4)
        tb.AddSpacer(18)
        tb.Add(self._can_dot,     flag=wx.ALIGN_CENTER_VERTICAL)
        tb.Add(self._can_text,    flag=wx.ALIGN_CENTER_VERTICAL | wx.LEFT,  border=4)
        tb.AddStretchSpacer()
        tb.Add(can_btn,   flag=wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border=6)
        tb.Add(clear_btn, flag=wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border=8)
        outer.Add(tb, flag=wx.EXPAND | wx.TOP | wx.BOTTOM, border=6)

        # ── splitter: sidebar | log ────────────────────────────────────
        splitter = wx.SplitterWindow(panel, style=wx.SP_LIVE_UPDATE | wx.SP_3DSASH)
        splitter.SetMinimumPaneSize(180)

        left  = self._build_sidebar(splitter)
        right = self._build_log_panel(splitter)

        splitter.SplitVertically(left, right, sashPosition=240)
        outer.Add(splitter, proportion=1, flag=wx.EXPAND | wx.LEFT | wx.RIGHT,
                  border=8)

        # ── manual command bar ─────────────────────────────────────────
        cmd_row = wx.BoxSizer(wx.HORIZONTAL)
        self._cmd_input = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER,
                                      size=(-1, 32))
        self._cmd_input.SetHint("manual command (e.g.  readaddr D0000000)")
        self._cmd_input.Bind(wx.EVT_TEXT_ENTER, self._on_run_manual)
        run_btn = wx.Button(panel, label="Run ↗", size=(80, 32))
        run_btn.Bind(wx.EVT_BUTTON, self._on_run_manual)

        cmd_row.Add(self._cmd_input, proportion=1,
                    flag=wx.ALIGN_CENTER_VERTICAL)
        cmd_row.Add(run_btn, flag=wx.ALIGN_CENTER_VERTICAL | wx.LEFT, border=6)
        outer.Add(cmd_row, flag=wx.EXPAND | wx.ALL, border=8)

        panel.SetSizer(outer)
        self._update_status()

    def _build_sidebar(self, parent) -> wx.Panel:
        sidebar = scrolled.ScrolledPanel(parent, style=wx.BORDER_NONE)
        sidebar.SetupScrolling(scroll_x=False)
        sizer = wx.BoxSizer(wx.VERTICAL)

        sections = [
            ("DEVICE", [
                ("Upload BSL",            self._on_upload),
                ("Reset ECU",             self._on_reset),
                ("Read Device ID",        self._on_deviceid),
            ]),
            ("SBOOT", [
                ("SBOOT Login",           self._on_sboot),
                ("SBOOT Send Key…",       self._on_sboot_sendkey),
                ("CRC Reset…",            self._on_sboot_crc_reset),
                ("Extract Boot Passwords",self._on_extract_passwords),
            ]),
            ("FLASH", [
                ("Flash Info",            self._on_flashinfo),
                ("Dump Mask ROM",         self._on_dumpmaskrom),
                ("Dump Memory…",          self._on_dumpmem),
                ("Erase Sector…",         self._on_erase_sector),
                ("Send Read Passwords…",  self._on_send_read_passwords),
                ("Send Write Passwords…", self._on_send_write_passwords),
            ]),
            ("MEMORY", [
                ("Read Address…",         self._on_readaddr),
                ("Write Address…",        self._on_writeaddr),
                ("Compressed Read…",      self._on_compressed_read),
                ("Write File…",           self._on_write_file),
            ]),
        ]

        for section_title, commands in sections:
            heading = wx.StaticText(sidebar, label=section_title)
            heading.SetForegroundColour(CLR_MUTED)
            font = heading.GetFont()
            font.SetPointSize(9)
            font.SetWeight(wx.FONTWEIGHT_BOLD)
            heading.SetFont(font)
            sizer.Add(heading, flag=wx.LEFT | wx.TOP, border=12)
            sizer.Add(wx.StaticLine(sidebar), flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP,
                      border=8)

            for label, handler in commands:
                btn = wx.Button(sidebar, label=label,
                                style=wx.BU_LEFT, size=(-1, 30))
                btn.Bind(wx.EVT_BUTTON, handler)
                sizer.Add(btn, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP,
                          border=8)

        sizer.AddSpacer(16)
        sidebar.SetSizer(sizer)
        return sidebar

    def _build_log_panel(self, parent) -> wx.Panel:
        log_panel = wx.Panel(parent)
        sizer = wx.BoxSizer(wx.VERTICAL)

        label = wx.StaticText(log_panel, label="OUTPUT LOG")
        label.SetForegroundColour(CLR_MUTED)
        font = label.GetFont()
        font.SetPointSize(9)
        font.SetWeight(wx.FONTWEIGHT_BOLD)
        label.SetFont(font)
        sizer.Add(label, flag=wx.LEFT | wx.TOP, border=8)

        self._log_ctrl = wx.TextCtrl(
            log_panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2 |
                  wx.TE_AUTO_URL | wx.HSCROLL,
        )
        mono_font = wx.Font(10, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL,
                            wx.FONTWEIGHT_NORMAL)
        self._log_ctrl.SetFont(mono_font)
        sizer.Add(self._log_ctrl, proportion=1, flag=wx.EXPAND | wx.ALL, border=8)

        log_panel.SetSizer(sizer)
        return log_panel

    # ------------------------------------------------------------------
    # Logging helpers
    # ------------------------------------------------------------------
    def _log(self, text: str, colour: wx.Colour | None = None):
        if not text.endswith("\n"):
            text += "\n"
        if colour:
            style = wx.TextAttr(colour)
            self._log_ctrl.SetDefaultStyle(style)
        self._log_ctrl.AppendText(text)
        if colour:
            self._log_ctrl.SetDefaultStyle(wx.TextAttr(wx.NullColour))
        self._log_ctrl.ShowPosition(self._log_ctrl.GetLastPosition())

    def _start_log_timer(self):
        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._drain_log_queue, self._timer)
        self._timer.Start(100)   # poll every 100 ms

    def _drain_log_queue(self, _evt=None):
        while not self._log_queue.empty():
            try:
                msg = self._log_queue.get_nowait()
                colour = CLR_SUCCESS if "Success" in msg else \
                         CLR_FAILURE if ("Fail" in msg or "ERROR" in msg or "FAIL" in msg) else \
                         CLR_INFO    if msg.startswith("→") else None
                self._log(msg, colour)
            except queue.Empty:
                break

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------
    def _update_status(self):
        if BSL_AVAILABLE:
            self._status_dot.SetForegroundColour(CLR_SUCCESS)
            self._status_text.SetLabel("BSL ready")
        else:
            self._status_dot.SetForegroundColour(CLR_FAILURE)
            self._status_text.SetLabel("BSL module unavailable (demo mode)")

    def _update_can_status(self, is_up: bool):
        if is_up:
            self._can_dot.SetForegroundColour(CLR_SUCCESS)
            self._can_text.SetLabel(f"CAN: {CAN_INTERFACE} UP")
        else:
            self._can_dot.SetForegroundColour(CLR_FAILURE)
            self._can_text.SetLabel(f"CAN: {CAN_INTERFACE} DOWN")

    def _bring_up_can_async(self):
        """Run CAN bring-up in a background thread, update status when done."""
        self._log(f"▶ Bringing up {CAN_INTERFACE} at {CAN_BITRATE} bps…", CLR_INFO)
        self._can_dot.SetForegroundColour(CLR_MUTED)
        self._can_text.SetLabel(f"CAN: bringing up…")

        def _work():
            results = bring_up_can()
            all_ok  = all(rc == 0 for _, rc, _ in results)
            for cmd_str, rc, stderr in results:
                self._log_queue.put(f"  $ {cmd_str}")
                if rc != 0:
                    self._log_queue.put(f"  ERROR (rc={rc}): {stderr or '(no output)'}")
                else:
                    self._log_queue.put(f"  OK")
            up = can_is_up()
            wx.CallAfter(self._update_can_status, up)
            if up:
                self._log_queue.put(f"CAN interface {CAN_INTERFACE} is UP.")
            else:
                self._log_queue.put(
                    f"CAN interface {CAN_INTERFACE} could not be brought up. "
                    f"Check that the interface exists and sudo is available."
                )

        threading.Thread(target=_work, daemon=True).start()

    def _on_bring_up_can(self, _evt):
        self._bring_up_can_async()

    def _set_busy(self, busy: bool):
        self._busy = busy

    # ------------------------------------------------------------------
    # Generic runner
    # ------------------------------------------------------------------
    def _run(self, fn, *args, label="", **kwargs):
        if not BSL_AVAILABLE:
            self._log(f"[demo] Would call: {label or fn.__name__}({args})", CLR_MUTED)
            return
        if self._busy:
            wx.MessageBox("A command is already running — please wait.",
                          "Busy", wx.OK | wx.ICON_WARNING)
            return
        self._set_busy(True)
        self._log(f"▶ {label or fn.__name__}…", CLR_INFO)

        def _done():
            self._set_busy(False)

        def _wrapper(*a, **kw):
            fn(*a, **kw)
            wx.CallAfter(_done)

        Worker(self._log_queue, _wrapper, *args, **kwargs).start()

    # ------------------------------------------------------------------
    # Event handlers — DEVICE
    # ------------------------------------------------------------------
    def _on_upload(self, _evt):
        self._run(bsl.upload_bsl, label="upload_bsl")

    def _on_reset(self, _evt):
        self._run(bsl.reset_ecu, label="reset_ecu")

    def _on_deviceid(self, _evt):
        def _read():
            device_id = bsl.read_device_id()
            if len(device_id) > 1:
                print("Device ID: " + device_id.hex())
            else:
                print("Failed to retrieve Device ID")
        self._run(_read, label="read_device_id")

    # ------------------------------------------------------------------
    # Event handlers — SBOOT
    # ------------------------------------------------------------------
    def _on_sboot(self, _evt):
        self._run(bsl.sboot_login, label="sboot_login")

    def _on_sboot_sendkey(self, _evt):
        dlg = InputDialog(self, "SBOOT Send Key",
                          [("Key data (hex)", "e.g. DEADBEEF")])
        if dlg.ShowModal() == wx.ID_OK:
            vals = dlg.get_values()
            key_data = bytearray.fromhex(vals["Key data (hex)"])
            self._run(bsl.sboot_sendkey, key_data, label="sboot_sendkey")
        dlg.Destroy()

    def _on_sboot_crc_reset(self, _evt):
        dlg = InputDialog(self, "SBOOT CRC Reset",
                          [("Address (hex)", "e.g. 8001420C")])
        if dlg.ShowModal() == wx.ID_OK:
            vals = dlg.get_values()
            addr = bytearray.fromhex(vals["Address (hex)"])
            self._run(bsl.sboot_crc_reset, addr, label="sboot_crc_reset")
        dlg.Destroy()

    def _on_extract_passwords(self, _evt):
        if wx.MessageBox(
            "This will run the full SBoot exploit chain.\nContinue?",
            "Confirm", wx.YES_NO | wx.ICON_WARNING
        ) == wx.YES:
            self._run(bsl.extract_boot_passwords, label="extract_boot_passwords")

    # ------------------------------------------------------------------
    # Event handlers — FLASH
    # ------------------------------------------------------------------
    def _on_flashinfo(self, _evt):
        def _read():
            PMU_BASE_ADDRS = {0: 0xF8001000, 1: 0xF8003000}
            for pmu_num in PMU_BASE_ADDRS:
                bsl.read_flash_properties(pmu_num, PMU_BASE_ADDRS[pmu_num])
        self._run(_read, label="flash_info")

    def _on_dumpmaskrom(self, _evt):
        with wx.FileDialog(self, "Save Mask ROM dump",
                           wildcard="Binary files (*.bin)|*.bin|All files (*)|*",
                           defaultFile="maskrom.bin",
                           style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as fd:
            if fd.ShowModal() == wx.ID_CANCEL:
                return
            filename = fd.GetPath()
        self._run(bsl.read_bytes_file, 0xAFFFC000, 0x4000, filename,
                  label="dump_maskrom")

    def _on_dumpmem(self, _evt):
        dlg = InputDialog(self, "Dump Memory", [
            ("Start address (hex)", "e.g. D0000000"),
            ("Size (hex)",          "e.g. 4000"),
        ])
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        v = dlg.get_values()
        dlg.Destroy()
        with wx.FileDialog(self, "Save memory dump",
                           wildcard="Binary files (*.bin)|*.bin|All files (*)|*",
                           defaultFile="dump.bin",
                           style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as fd:
            if fd.ShowModal() == wx.ID_CANCEL:
                return
            filename = fd.GetPath()
        self._run(bsl.read_bytes_file,
                  int(v["Start address (hex)"], 16),
                  int(v["Size (hex)"], 16),
                  filename,
                  label="dump_mem")

    def _on_erase_sector(self, _evt):
        dlg = InputDialog(self, "Erase Sector",
                          [("Sector address (hex)", "e.g. 80014000")])
        if dlg.ShowModal() == wx.ID_OK:
            vals = dlg.get_values()
            addr = bytearray.fromhex(vals["Sector address (hex)"])
            self._run(bsl.erase_sector, addr, label="erase_sector")
        dlg.Destroy()

    def _on_send_read_passwords(self, _evt):
        dlg = InputDialog(self, "Send Read Passwords", [
            ("PW1 (hex)", "e.g. DEADBEEF"),
            ("PW2 (hex)", "e.g. CAFEBABE"),
        ])
        if dlg.ShowModal() == wx.ID_OK:
            v = dlg.get_values()
            pw1 = int.from_bytes(bytearray.fromhex(v["PW1 (hex)"]), "big").to_bytes(4, "little")
            pw2 = int.from_bytes(bytearray.fromhex(v["PW2 (hex)"]), "big").to_bytes(4, "little")
            self._run(bsl.send_passwords, pw1, pw2, label="send_read_passwords")
        dlg.Destroy()

    def _on_send_write_passwords(self, _evt):
        dlg = InputDialog(self, "Send Write Passwords", [
            ("PW1 (hex)", "e.g. DEADBEEF"),
            ("PW2 (hex)", "e.g. CAFEBABE"),
        ])
        if dlg.ShowModal() == wx.ID_OK:
            v = dlg.get_values()
            pw1 = int.from_bytes(bytearray.fromhex(v["PW1 (hex)"]), "big").to_bytes(4, "little")
            pw2 = int.from_bytes(bytearray.fromhex(v["PW2 (hex)"]), "big").to_bytes(4, "little")
            self._run(bsl.send_passwords, pw1, pw2, read_write=0x05, ucb=1,
                      label="send_write_passwords")
        dlg.Destroy()

    # ------------------------------------------------------------------
    # Event handlers — MEMORY
    # ------------------------------------------------------------------
    def _on_readaddr(self, _evt):
        dlg = InputDialog(self, "Read Address",
                          [("Address (hex)", "e.g. D0000000")])
        if dlg.ShowModal() == wx.ID_OK:
            vals = dlg.get_values()
            addr = bytearray.fromhex(vals["Address (hex)"])

            def _read():
                data = bsl.read_byte(addr)
                print(f"0x{vals['Address (hex)']} → {data.hex()}")

            self._run(_read, label="read_addr")
        dlg.Destroy()

    def _on_writeaddr(self, _evt):
        dlg = InputDialog(self, "Write Address", [
            ("Address (hex)", "e.g. D0000000"),
            ("Value (hex)",   "e.g. DEADBEEF"),
        ])
        if dlg.ShowModal() == wx.ID_OK:
            v = dlg.get_values()
            addr  = bytearray.fromhex(v["Address (hex)"])
            value = bytearray.fromhex(v["Value (hex)"])

            def _write():
                ok = bsl.write_byte(addr, value)
                print("Wrote " + v["Value (hex)"] + " to " + v["Address (hex)"]
                      if ok else "Write failed.")

            self._run(_write, label="write_addr")
        dlg.Destroy()

    def _on_compressed_read(self, _evt):
        dlg = InputDialog(self, "Compressed Read", [
            ("Address (hex)", "e.g. 80000000"),
            ("Length (hex)",  "e.g. 00100000"),
        ])
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        v = dlg.get_values()
        dlg.Destroy()
        with wx.FileDialog(self, "Save compressed read output",
                           wildcard="Binary files (*.bin)|*.bin|All files (*)|*",
                           defaultFile="flash.bin",
                           style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as fd:
            if fd.ShowModal() == wx.ID_CANCEL:
                return
            filename = fd.GetPath()
        addr = bytearray.fromhex(v["Address (hex)"])
        size = bytearray.fromhex(v["Length (hex)"])
        self._run(bsl.read_compressed, addr, size, filename,
                  label="compressed_read")

    def _on_write_file(self, _evt):
        with wx.FileDialog(self, "Choose file to write",
                           wildcard="Binary files (*.bin)|*.bin|All files (*)|*",
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as fd:
            if fd.ShowModal() == wx.ID_CANCEL:
                return
            filename = fd.GetPath()
        dlg = InputDialog(self, "Write File", [
            ("Address (hex)", "e.g. 80000000"),
            ("Length (hex)",  "e.g. 00100000"),
        ])
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        v = dlg.get_values()
        dlg.Destroy()
        addr = bytearray.fromhex(v["Address (hex)"])
        size = bytearray.fromhex(v["Length (hex)"])
        self._run(bsl.write_file, addr, size, filename,
                  label="write_file")

    # ------------------------------------------------------------------
    # Manual command bar  (mirrors BootloaderRepl dispatch)
    # ------------------------------------------------------------------
    def _on_run_manual(self, _evt):
        raw = self._cmd_input.GetValue().strip()
        if not raw:
            return
        self._log(f"(BSL) {raw}", CLR_MUTED)
        self._cmd_input.Clear()

        # Re-use the existing cmd.Cmd object for parsing
        if not BSL_AVAILABLE:
            self._log("[demo] REPL unavailable.", CLR_MUTED)
            return

        repl = bsl.BootloaderRepl()

        def _dispatch():
            try:
                repl.onecmd(raw)
            except Exception as exc:
                print(f"ERROR: {exc}")

        Worker(self._log_queue, _dispatch).start()

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------
    def _on_clear(self, _evt):
        self._log_ctrl.Clear()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    app = wx.App(False)
    MainFrame()
    app.MainLoop()


if __name__ == "__main__":
    main()
