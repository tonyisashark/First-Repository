"""Tkinter desktop GUI for the Kalshi temperature trading bot.

A control panel with two tabs:
  * Dashboard -- Start/Stop, live status (state, position, balance, mode) and a
    streaming activity log.
  * Settings  -- every strategy / connection knob, persisted to a per-user
    ``.env`` so the installed app remembers them.

The bot runs on a background daemon thread; the UI stays responsive and is
refreshed from a ``root.after`` poll loop.  Log records flow to the UI through a
thread-safe queue.
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Dict

from . import money
from .config import Config, save_settings
from .factory import build_auth, build_bot, build_client

logger = logging.getLogger("kalshi_temp_bot")

APP_TITLE = "Kalshi Temperature Trading Bot"

# (env key, label, kind, choices) -- drives both the Settings form and saving.
# Only operator-level choices live here; the probability model, edge thresholds
# and timing are self-tuned inside the strategy and need no input.
SETTINGS_FIELDS = [
    ("KALSHI_ENV", "Environment", "choice", ["demo", "prod"]),
    ("DRY_RUN", "Dry run (paper trading -- no real orders)", "bool", None),
    ("KALSHI_API_KEY_ID", "API Key ID", "text", None),
    ("KALSHI_PRIVATE_KEY_PATH", "Private key (.pem) path", "file", None),
    ("TEMPERATURE_SERIES", "Temperature series (comma-separated, blank = defaults)", "text", None),
    ("PORTFOLIO_FRACTION", "Max bankroll fraction per trade (1/3)", "text", None),
    ("MAX_POSITIONS", "Max concurrent positions", "text", None),
    ("PAPER_BALANCE_CENTS", "Paper-trading balance (cents)", "text", None),
]


def cfg_to_env_values(cfg: Config) -> Dict[str, str]:
    """Flatten a Config into the env-var string values the form/save use."""
    return {
        "KALSHI_ENV": cfg.env,
        "DRY_RUN": "true" if cfg.dry_run else "false",
        "KALSHI_API_KEY_ID": cfg.api_key_id or "",
        "KALSHI_PRIVATE_KEY_PATH": cfg.private_key_path or "",
        "TEMPERATURE_SERIES": ",".join(cfg.temperature_series),
        "PORTFOLIO_FRACTION": f"{cfg.portfolio_fraction:.10g}",
        "MAX_POSITIONS": str(cfg.max_positions),
        "PAPER_BALANCE_CENTS": str(cfg.paper_balance_cents),
    }


def resource_path(relative: str) -> str:
    """Resolve a bundled resource path (works under PyInstaller's onefile)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, relative)


class _QueueLogHandler(logging.Handler):
    def __init__(self, log_queue: "queue.Queue[str]") -> None:
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.log_queue.put_nowait(self.format(record))
        except Exception:
            pass


class BotGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.bot = None
        self.bot_thread = None
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.vars: Dict[str, tk.Variable] = {}
        self.active_cfg = Config.from_env()
        self._balance_cents = None

        self._build_ui()
        self._set_icon()
        self._populate_from_cfg(self.active_cfg)
        self._install_log_handler()

        logger.info("GUI ready. Configure settings, then press Start.")
        self.root.after(300, self._poll)

    # -- UI construction ---------------------------------------------------
    def _build_ui(self) -> None:
        self.root.minsize(720, 560)
        header = ttk.Frame(self.root, padding=(12, 10))
        header.pack(fill="x")
        ttk.Label(header, text=APP_TITLE, font=("Segoe UI", 14, "bold")).pack(side="left")
        self.mode_badge = ttk.Label(header, text="", font=("Segoe UI", 10, "bold"))
        self.mode_badge.pack(side="right")

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self.dashboard = ttk.Frame(notebook, padding=10)
        self.settings = ttk.Frame(notebook, padding=10)
        notebook.add(self.dashboard, text="Dashboard")
        notebook.add(self.settings, text="Settings")
        self._build_dashboard(self.dashboard)
        self._build_settings(self.settings)

        self.status_bar = ttk.Label(self.root, text="Ready", relief="sunken", anchor="w", padding=(8, 3))
        self.status_bar.pack(fill="x", side="bottom")

    def _build_dashboard(self, parent: ttk.Frame) -> None:
        controls = ttk.Frame(parent)
        controls.pack(fill="x", pady=(0, 8))
        self.start_btn = ttk.Button(controls, text="▶  Start", command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(controls, text="■  Stop", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        ttk.Button(controls, text="↻  Refresh balance", command=self._refresh_balance).pack(side="left", padx=6)

        self.warning_label = ttk.Label(parent, text="", foreground="#b00020", font=("Segoe UI", 10, "bold"))
        self.warning_label.pack(fill="x", pady=(0, 4))

        grid = ttk.LabelFrame(parent, text="Status", padding=10)
        grid.pack(fill="x")
        self.status_vars: Dict[str, tk.StringVar] = {}
        rows = [
            ("State", "state"), ("Mode", "mode"), ("Environment", "env"),
            ("Markets tracked", "markets"), ("Position", "position"), ("Balance", "balance"),
        ]
        for i, (label, key) in enumerate(rows):
            r, c = divmod(i, 2)
            ttk.Label(grid, text=label + ":", width=16, anchor="w").grid(row=r, column=c * 2, sticky="w", padx=4, pady=3)
            var = tk.StringVar(value="-")
            self.status_vars[key] = var
            ttk.Label(grid, textvariable=var, width=24, anchor="w", font=("Segoe UI", 10, "bold")).grid(
                row=r, column=c * 2 + 1, sticky="w", padx=4, pady=3)

        log_frame = ttk.LabelFrame(parent, text="Activity log", padding=6)
        log_frame.pack(fill="both", expand=True, pady=(8, 0))
        self.log_text = tk.Text(log_frame, height=12, wrap="none", state="disabled",
                                background="#101418", foreground="#d7dde3", font=("Consolas", 9))
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

    def _build_settings(self, parent: ttk.Frame) -> None:
        form = ttk.Frame(parent)
        form.pack(fill="both", expand=True)
        form.columnconfigure(1, weight=1)

        for row, (key, label, kind, choices) in enumerate(SETTINGS_FIELDS):
            ttk.Label(form, text=label, anchor="w").grid(row=row, column=0, sticky="w", padx=4, pady=4)
            if kind == "bool":
                var: tk.Variable = tk.BooleanVar()
                ttk.Checkbutton(form, variable=var).grid(row=row, column=1, sticky="w", padx=4)
            elif kind == "choice":
                var = tk.StringVar()
                ttk.Combobox(form, textvariable=var, values=choices, state="readonly", width=20).grid(
                    row=row, column=1, sticky="w", padx=4)
            elif kind == "file":
                var = tk.StringVar()
                cell = ttk.Frame(form)
                cell.grid(row=row, column=1, sticky="ew", padx=4)
                cell.columnconfigure(0, weight=1)
                ttk.Entry(cell, textvariable=var).grid(row=0, column=0, sticky="ew")
                ttk.Button(cell, text="Browse…", command=lambda v=var: self._browse_key(v)).grid(row=0, column=1, padx=(4, 0))
            else:
                var = tk.StringVar()
                show = "*" if key == "KALSHI_API_KEY_ID" else None
                ttk.Entry(form, textvariable=var, show=show).grid(row=row, column=1, sticky="ew", padx=4)
            self.vars[key] = var

        btns = ttk.Frame(parent)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Save settings", command=self._save).pack(side="left")
        ttk.Button(btns, text="Reload", command=self._reload).pack(side="left", padx=6)
        ttk.Label(parent, text="Settings are saved per-user (so they survive across runs). "
                              "Credentials never leave your machine.",
                  foreground="#666").pack(anchor="w", pady=(8, 0))

    def _set_icon(self) -> None:
        try:
            self.root.iconbitmap(resource_path(os.path.join("assets", "icon.ico")))
        except Exception:
            pass  # icon is cosmetic; non-Windows or missing file is fine

    def _install_log_handler(self) -> None:
        handler = _QueueLogHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s", datefmt="%H:%M:%S"))
        handler.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

    # -- settings <-> form -------------------------------------------------
    def _populate_from_cfg(self, cfg: Config) -> None:
        values = cfg_to_env_values(cfg)
        for key, var in self.vars.items():
            value = values.get(key, "")
            if isinstance(var, tk.BooleanVar):
                var.set(str(value).lower() in ("1", "true", "yes", "on"))
            else:
                var.set(value)

    def _gather_env_values(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for key, var in self.vars.items():
            value = var.get()
            if isinstance(value, bool):
                value = "true" if value else "false"
            out[key] = str(value)
        return out

    def _collect_cfg(self) -> Config:
        for key, value in self._gather_env_values().items():
            os.environ[key] = value
        return Config.from_env()

    def _browse_key(self, var: tk.StringVar) -> None:
        path = filedialog.askopenfilename(
            title="Select RSA private key",
            filetypes=[("PEM private key", "*.pem"), ("All files", "*.*")],
        )
        if path:
            var.set(path)

    def _save(self) -> None:
        try:
            path = save_settings(self._gather_env_values())
            self.status_bar.config(text=f"Settings saved to {path}")
            messagebox.showinfo("Saved", f"Settings saved to:\n{path}")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Save failed", str(exc))

    def _reload(self) -> None:
        self._populate_from_cfg(Config.from_env())
        self.status_bar.config(text="Settings reloaded from disk")

    # -- bot lifecycle -----------------------------------------------------
    def _start(self) -> None:
        if self.bot is not None and getattr(self.bot, "_running", False):
            return
        cfg = self._collect_cfg()

        if not cfg.dry_run:
            if build_auth(cfg) is None:
                messagebox.showerror(
                    "Cannot start live trading",
                    "Live trading (Dry run unchecked) requires a valid API Key ID and private key.\n\n"
                    "Either fill in your credentials or re-enable Dry run.",
                )
                return
            warn = ("You are about to trade with REAL MONEY"
                    f"{' on PRODUCTION' if cfg.env == 'prod' else ''}.\n\nContinue?")
            if not messagebox.askyesno("Confirm live trading", warn, icon="warning"):
                return

        self.active_cfg = cfg
        try:
            self.bot, _ = build_bot(cfg)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Failed to start", str(exc))
            return

        self.bot_thread = threading.Thread(target=self._run_bot, name="bot-runner", daemon=True)
        self.bot_thread.start()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_bar.config(text="Bot running")

    def _run_bot(self) -> None:
        try:
            self.bot.run()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Bot crashed: %s", exc)
        finally:
            logger.info("Bot stopped.")

    def _stop(self) -> None:
        if self.bot is not None:
            self.bot.stop()
        self.stop_btn.config(state="disabled")
        self.status_bar.config(text="Stopping…")

    def _refresh_balance(self) -> None:
        cfg = self._collect_cfg()

        def work() -> None:
            try:
                auth = build_auth(cfg)
                if auth is None:
                    logger.info("Balance: no API credentials configured.")
                    return
                data = build_client(cfg, auth).get_balance()
                self._balance_cents = money.balance_cents(data)
                logger.info("Balance refreshed: $%.2f", self._balance_cents / 100)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Balance refresh failed: %s", exc)

        threading.Thread(target=work, daemon=True).start()

    # -- periodic UI refresh ----------------------------------------------
    def _poll(self) -> None:
        drained = 0
        while drained < 300:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self._append_log(line)
            drained += 1
        try:
            self._update_status()
        except Exception:  # noqa: BLE001 - never let the UI loop die
            logger.debug("status update error", exc_info=True)
        self.root.after(300, self._poll)

    def _append_log(self, line: str) -> None:
        self.log_text.config(state="normal")
        self.log_text.insert("end", line + "\n")
        # Trim to keep memory bounded.
        if int(self.log_text.index("end-1c").split(".")[0]) > 600:
            self.log_text.delete("1.0", "100.end")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _update_status(self) -> None:
        cfg = self.active_cfg
        running = self.bot is not None and getattr(self.bot, "_running", False)
        live = not cfg.dry_run

        self.status_vars["mode"].set("LIVE (real money)" if live else "Paper (dry run)")
        self.status_vars["env"].set(cfg.env)
        self.mode_badge.config(
            text=("● LIVE" if live else "● PAPER"),
            foreground=("#b00020" if live else "#1a7f37"),
        )
        self.warning_label.config(
            text="LIVE TRADING — real orders will be placed." if (live and running) else "")

        if self.bot is None:
            self.status_vars["state"].set("stopped")
            self.status_vars["markets"].set("-")
            self.status_vars["position"].set("none")
        else:
            trades = list(getattr(self.bot, "trades", []))
            if not running:
                self.status_vars["state"].set("stopped")
            else:
                self.status_vars["state"].set(
                    "idle" if not trades else f"{len(trades)} position(s)")
            try:
                self.status_vars["markets"].set(str(len(self.bot.current_tickers())))
            except Exception:
                self.status_vars["markets"].set("-")
            if not trades:
                self.status_vars["position"].set("none")
            else:
                shown = ", ".join(f"{t.ticker} {t.side.upper()} x{t.count}" for t in trades[:2])
                if len(trades) > 2:
                    shown += f" +{len(trades) - 2}"
                self.status_vars["position"].set(
                    f"{shown}  (max {cfg.max_positions})")

        self.status_vars["balance"].set(
            "-" if self._balance_cents is None else f"${self._balance_cents / 100:,.2f}")

        if not running and self.bot is not None and self.start_btn["state"] == "disabled":
            # Bot finished/stopped on its own; re-enable Start.
            self.start_btn.config(state="normal")
            self.stop_btn.config(state="disabled")

    # -- shutdown ----------------------------------------------------------
    def on_close(self) -> None:
        if self.bot is not None and getattr(self.bot, "_running", False):
            if not messagebox.askokcancel("Quit", "The bot is running. Stop it and quit?"):
                return
            self.bot.stop()
        self.root.destroy()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    root = tk.Tk()
    root.title(APP_TITLE)
    try:
        root.geometry("840x660")
    except Exception:
        pass
    app = BotGUI(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
