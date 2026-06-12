"""Tkinter desktop app: dashboard, equity chart, logs, settings, kill switch.

Run with ``kalshi-bot gui`` (or the packaged ``KalshiBot.exe``). The trading
loop runs on a worker thread; the GUI communicates through queues and reads
the SQLite state with its own connections, so the two sides never share
objects that aren't thread-safe. The bot (and its SQLite connection) is
*built inside* the worker thread for the same reason.
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from typing import Dict, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .config import Config, LIVE_ACK_PHRASE
from .gui_settings import (FIELDS, GROUPS, export_to_environ, initial_values,
                           load_settings, normalize_bool, save_settings)
from .money import micro_to_display
from .paths import default_db_path, ensure_data_dir, log_path, settings_path, user_data_dir
from .risk import RiskManager
from .state import StateStore

logger = logging.getLogger(__name__)

REFRESH_MS = 700
CHART_REFRESH_S = 5.0
CHART_WINDOW_S = 24 * 3600
MAX_LOG_LINES = 2000

BG = "#101418"
PANEL = "#1a2027"
FG = "#d7dde3"
ACCENT = "#37b26c"
RED = "#d4533b"
AMBER = "#d9a23b"


class QueueLogHandler(logging.Handler):
    def __init__(self, q: "queue.Queue[str]") -> None:
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put_nowait(self.format(record))
        except Exception:
            pass


class BotRunner:
    """Owns the worker thread; the bot and its DB connection live inside it."""

    def __init__(self) -> None:
        self.thread: Optional[threading.Thread] = None
        self.bot = None
        self.error: Optional[str] = None
        self.status_q: "queue.Queue[dict]" = queue.Queue()
        self._stop_requested = False

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, cfg: Config) -> None:
        from .bot import build_bot

        self.error = None
        self._stop_requested = False

        def target() -> None:
            try:
                bot = build_bot(cfg)
                bot.status_listener = self.status_q.put
                self.bot = bot
                if self._stop_requested:
                    bot.stop()
                bot.run_forever()
            except Exception as exc:
                self.error = str(exc)
                logger.exception("bot thread crashed")
            finally:
                self.bot = None

        self.thread = threading.Thread(target=target, name="kalshi-bot",
                                       daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop_requested = True
        bot = self.bot
        if bot is not None:
            bot.stop()


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.runner = BotRunner()
        self.log_q: "queue.Queue[str]" = queue.Queue()
        self.vars: Dict[str, tk.Variable] = {}
        self.last_status: Optional[dict] = None
        self._chart_due = 0.0
        self._stopping = False

        self._setup_logging()
        self._build_window()
        self._load_settings_into_form()
        self.root.after(REFRESH_MS, self._tick)
        logger.info("kalshi-bot GUI v%s ready (data dir: %s)",
                    __version__, user_data_dir())

    # ------------------------------------------------------------- logging
    def _setup_logging(self) -> None:
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                                datefmt="%H:%M:%S")
        qh = QueueLogHandler(self.log_q)
        qh.setFormatter(fmt)
        root.addHandler(qh)
        try:
            fh = logging.FileHandler(log_path(), encoding="utf-8")
            fh.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
            root.addHandler(fh)
        except OSError:
            pass
        logging.getLogger("urllib3").setLevel(logging.WARNING)

    # ------------------------------------------------------------ UI build
    def _build_window(self) -> None:
        self.root.title(f"Kalshi Bot v{__version__}")
        self.root.geometry("1020x680")
        self.root.minsize(860, 560)
        self.root.configure(bg=BG)
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=FG, fieldbackground=PANEL)
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(14, 6))
        style.configure("TLabelframe", background=BG, foreground=FG)
        style.configure("TLabelframe.Label", background=BG, foreground=FG)
        style.configure("Stat.TLabel", background=BG, foreground=FG,
                        font=("Segoe UI", 10))
        style.configure("Big.TLabel", background=BG, foreground=FG,
                        font=("Segoe UI", 22, "bold"))
        style.configure("Mode.TLabel", font=("Segoe UI", 11, "bold"), padding=6)

        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=10, pady=(10, 4))
        self.mode_label = ttk.Label(top, text="", style="Mode.TLabel")
        self.mode_label.pack(side="left")
        self.start_btn = ttk.Button(top, text="Start", command=self._on_start_stop)
        self.start_btn.pack(side="right", padx=4)
        ttk.Button(top, text="Kill switch", command=self._on_kill).pack(side="right", padx=4)
        ttk.Button(top, text="Resume", command=self._on_resume).pack(side="right", padx=4)
        ttk.Button(top, text="Reset paper", command=self._on_paper_reset).pack(side="right", padx=4)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(2, 10))
        self._build_dashboard(self.notebook)
        self._build_settings(self.notebook)
        self._build_help(self.notebook)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_dashboard(self, notebook: ttk.Notebook) -> None:
        page = ttk.Frame(notebook)
        notebook.add(page, text="  Dashboard  ")

        stats = ttk.Frame(page)
        stats.pack(fill="x", pady=(8, 4), padx=8)
        self.equity_label = ttk.Label(stats, text="$ --", style="Big.TLabel")
        self.equity_label.grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 24))
        self.stat_labels: Dict[str, ttk.Label] = {}
        items = [("day", "Day P&L"), ("dd", "Drawdown"), ("cash", "Cash"),
                 ("mtm", "Positions value"), ("escrow", "Resting escrow"),
                 ("pos", "Positions"), ("orders", "Orders"),
                 ("arb", "Arb used"), ("longshot", "Favorites used"),
                 ("mm", "MM used")]
        for i, (key, label) in enumerate(items):
            col, row = 1 + i % 5, i // 5
            cell = ttk.Frame(stats)
            cell.grid(row=row, column=col, sticky="w", padx=10, pady=2)
            ttk.Label(cell, text=label, style="Stat.TLabel",
                      foreground="#8a949e").pack(anchor="w")
            value = ttk.Label(cell, text="--", style="Stat.TLabel")
            value.pack(anchor="w")
            self.stat_labels[key] = value

        self.halt_label = ttk.Label(page, text="", style="Stat.TLabel",
                                    foreground=RED)
        self.halt_label.pack(fill="x", padx=10)

        middle = ttk.Frame(page)
        middle.pack(fill="both", expand=True, padx=8, pady=4)
        middle.rowconfigure(1, weight=1)
        middle.columnconfigure(0, weight=1)
        ttk.Label(middle, text="Equity -- last 24h", style="Stat.TLabel",
                  foreground="#8a949e").grid(row=0, column=0, sticky="w")
        self.chart = tk.Canvas(middle, height=160, bg=PANEL, highlightthickness=0)
        self.chart.grid(row=0, column=0, sticky="ew", pady=(22, 6))

        log_frame = ttk.Frame(middle)
        log_frame.grid(row=1, column=0, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, bg=PANEL, fg=FG, wrap="none",
                                state="disabled", font=("Consolas", 9),
                                relief="flat")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)

    def _build_settings(self, notebook: ttk.Notebook) -> None:
        page = ttk.Frame(notebook)
        notebook.add(page, text="  Settings  ")
        grid = ttk.Frame(page)
        grid.pack(fill="both", expand=True, padx=10, pady=8)
        for col in (0, 1):
            grid.columnconfigure(col, weight=1, uniform="settings")

        frames: Dict[str, ttk.Labelframe] = {}
        placement = {"Connection": (0, 0), "Safety": (1, 0), "Strategies": (0, 1),
                     "Risk": (1, 1), "Advanced": (2, 0)}
        for group in GROUPS:
            frame = ttk.Labelframe(grid, text=f" {group} ")
            row, col = placement[group]
            span = 2 if group == "Advanced" else 1
            frame.grid(row=row, column=col, columnspan=span, sticky="nsew",
                       padx=6, pady=6)
            frame.columnconfigure(1, weight=1)
            frames[group] = frame

        row_in: Dict[str, int] = {g: 0 for g in GROUPS}
        for field in FIELDS:
            frame = frames[field.group]
            row = row_in[field.group]
            row_in[field.group] += 1
            if field.kind == "bool":
                var = tk.BooleanVar(value=normalize_bool(field.default))
                widget = ttk.Checkbutton(frame, text=field.label, variable=var)
                widget.grid(row=row, column=0, columnspan=2, sticky="w",
                            padx=8, pady=3)
            else:
                ttk.Label(frame, text=field.label, style="Stat.TLabel").grid(
                    row=row, column=0, sticky="w", padx=8, pady=3)
                var = tk.StringVar(value=field.default)
                if field.kind == "choice":
                    widget = ttk.Combobox(frame, textvariable=var,
                                          values=list(field.choices),
                                          state="readonly", width=18)
                    widget.grid(row=row, column=1, sticky="w", padx=8)
                elif field.kind == "path":
                    cell = ttk.Frame(frame)
                    cell.grid(row=row, column=1, sticky="ew", padx=8)
                    cell.columnconfigure(0, weight=1)
                    ttk.Entry(cell, textvariable=var).grid(row=0, column=0,
                                                           sticky="ew")
                    ttk.Button(cell, text="...", width=3,
                               command=lambda v=var: self._browse(v)).grid(
                        row=0, column=1, padx=(4, 0))
                else:
                    ttk.Entry(frame, textvariable=var, width=22).grid(
                        row=row, column=1, sticky="w", padx=8)
            if field.help:
                row = row_in[field.group]
                row_in[field.group] += 1
                ttk.Label(frame, text=field.help, style="Stat.TLabel",
                          foreground="#76808a").grid(row=row, column=0,
                                                     columnspan=2, sticky="w",
                                                     padx=24)
            self.vars[field.key] = var

        bar = ttk.Frame(page)
        bar.pack(fill="x", padx=14, pady=(0, 10))
        ttk.Button(bar, text="Save settings", command=self._on_save).pack(side="left")
        ttk.Button(bar, text="Open data folder",
                   command=self._open_data_dir).pack(side="left", padx=8)
        self.save_note = ttk.Label(bar, text="", style="Stat.TLabel",
                                   foreground=ACCENT)
        self.save_note.pack(side="left", padx=8)
        ttk.Label(bar, text="Changes apply the next time the bot starts.",
                  style="Stat.TLabel", foreground="#76808a").pack(side="right")

    def _build_help(self, notebook: ttk.Notebook) -> None:
        page = ttk.Frame(notebook)
        notebook.add(page, text="  Help  ")
        text = tk.Text(page, bg=BG, fg=FG, wrap="word", relief="flat",
                       font=("Segoe UI", 10), padx=16, pady=12)
        text.pack(fill="both", expand=True)
        text.insert("1.0", HELP_TEXT.format(ack=LIVE_ACK_PHRASE,
                                            data=user_data_dir()))
        text.configure(state="disabled")
        link = ttk.Label(page, text="Kalshi API docs: docs.kalshi.com",
                         style="Stat.TLabel", foreground="#6ab0de", cursor="hand2")
        link.pack(anchor="w", padx=16, pady=(0, 10))
        link.bind("<Button-1>", lambda _e: webbrowser.open("https://docs.kalshi.com"))

    # ----------------------------------------------------------- settings io
    def _load_settings_into_form(self) -> None:
        values = initial_values(load_settings(settings_path()))
        for key, value in values.items():
            var = self.vars.get(key)
            if var is None:
                continue
            if isinstance(var, tk.BooleanVar):
                var.set(normalize_bool(value))
            else:
                var.set(value)

    def _form_values(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for field in FIELDS:
            var = self.vars[field.key]
            if isinstance(var, tk.BooleanVar):
                out[field.key] = "true" if var.get() else "false"
            else:
                out[field.key] = str(var.get()).strip()
        return out

    def _validate_form(self, values: Dict[str, str]) -> Optional[str]:
        for field in FIELDS:
            if field.kind == "number" and values.get(field.key, "") != "":
                try:
                    float(values[field.key])
                except ValueError:
                    return f"{field.label}: not a number ({values[field.key]!r})"
        return None

    def _on_save(self) -> None:
        values = self._form_values()
        problem = self._validate_form(values)
        if problem:
            messagebox.showerror("Settings", problem, parent=self.root)
            return
        save_settings(settings_path(), values)
        self.save_note.configure(text=f"saved to {settings_path()}")
        self.root.after(4000, lambda: self.save_note.configure(text=""))

    def _browse(self, var: tk.StringVar) -> None:
        chosen = filedialog.askopenfilename(parent=self.root)
        if chosen:
            var.set(chosen)

    def _open_data_dir(self) -> None:
        path = str(ensure_data_dir())
        try:
            if sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except OSError as exc:
            messagebox.showinfo("Data folder", f"{path}\n({exc})", parent=self.root)

    # ----------------------------------------------------------------- run
    def _on_start_stop(self) -> None:
        if self.runner.running:
            self._stopping = True
            self.start_btn.configure(text="Stopping...", state="disabled")
            self.runner.stop()
            return
        values = self._form_values()
        problem = self._validate_form(values)
        if problem:
            messagebox.showerror("Settings", problem, parent=self.root)
            return
        save_settings(settings_path(), values)
        export_to_environ(values)
        if not os.environ.get("STATE_DB_PATH"):
            os.environ["STATE_DB_PATH"] = str(default_db_path())
        try:
            cfg = Config.from_env()
            cfg.validate()
        except ValueError as exc:
            messagebox.showerror("Cannot start", str(exc), parent=self.root)
            return
        if cfg.live_trading:
            sure = messagebox.askokcancel(
                "Real money",
                "You are about to trade REAL MONEY on the production exchange.\n\n"
                "The bot will place orders autonomously under the configured "
                "risk limits. Losses are possible and yours alone.\n\nContinue?",
                icon="warning", parent=self.root)
            if not sure:
                return
        logging.getLogger().setLevel(
            getattr(logging, cfg.log_level, logging.INFO))
        ensure_data_dir()
        self.runner.start(cfg)
        self.start_btn.configure(text="Stop")
        logger.info("bot starting (%s, %s)", cfg.env,
                    "paper" if cfg.dry_run else "LIVE ORDERS")

    def _on_kill(self) -> None:
        if not messagebox.askokcancel(
                "Kill switch",
                "Halt all trading and cancel resting orders?", parent=self.root):
            return
        try:
            cfg = Config.from_env()
            state = StateStore(self._db_path())
            RiskManager(cfg, state).kill("GUI kill switch")
            state.close()
        except Exception as exc:
            messagebox.showerror("Kill switch", str(exc), parent=self.root)
            return
        note = ("Halted. The running bot cancels its resting orders within "
                "one cycle.") if self.runner.running else \
            "Halted. Start the bot (it stays halted) or use the CLI to cancel orders."
        logger.warning("KILL SWITCH: %s", note)

    def _on_resume(self) -> None:
        try:
            cfg = Config.from_env()
            state = StateStore(self._db_path())
            RiskManager(cfg, state).resume()
            state.close()
            logger.info("halts cleared; trading allowed again")
        except Exception as exc:
            messagebox.showerror("Resume", str(exc), parent=self.root)

    def _on_paper_reset(self) -> None:
        if self.runner.running:
            messagebox.showinfo("Reset paper", "Stop the bot first.",
                                parent=self.root)
            return
        if not messagebox.askokcancel(
                "Reset paper",
                "Wipe the simulated portfolio and start fresh?", parent=self.root):
            return
        state = StateStore(self._db_path())
        state.paper_reset()
        state.close()
        logger.info("paper portfolio reset")

    def _db_path(self) -> str:
        return os.environ.get("STATE_DB_PATH") or str(default_db_path())

    # ---------------------------------------------------------------- ticks
    def _tick(self) -> None:
        try:
            self._drain_logs()
            self._drain_status()
            self._update_mode_banner()
            self._update_buttons()
            if time.monotonic() >= self._chart_due:
                self._chart_due = time.monotonic() + CHART_REFRESH_S
                self._redraw_chart()
        except Exception:
            logger.exception("GUI refresh failed")
        self.root.after(REFRESH_MS, self._tick)

    def _drain_logs(self) -> None:
        lines = []
        while True:
            try:
                lines.append(self.log_q.get_nowait())
            except queue.Empty:
                break
        if not lines:
            return
        self.log_text.configure(state="normal")
        self.log_text.insert("end", "\n".join(lines) + "\n")
        overflow = int(float(self.log_text.index("end-1c").split(".")[0])) - MAX_LOG_LINES
        if overflow > 0:
            self.log_text.delete("1.0", f"{overflow + 1}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _drain_status(self) -> None:
        status = None
        while True:
            try:
                status = self.runner.status_q.get_nowait()
            except queue.Empty:
                break
        if status is None:
            return
        self.last_status = status
        self.equity_label.configure(text=micro_to_display(status["equity"]))
        day = status["day_pnl"]
        self.stat_labels["day"].configure(
            text=f"{micro_to_display(day)} ({status['day_frac'] * 100:+.2f}%)",
            foreground=ACCENT if day >= 0 else RED)
        self.stat_labels["dd"].configure(
            text=f"{status['drawdown_frac'] * 100:.1f}%")
        self.stat_labels["cash"].configure(text=micro_to_display(status["cash"]))
        self.stat_labels["mtm"].configure(text=micro_to_display(status["mtm"]))
        self.stat_labels["escrow"].configure(text=micro_to_display(status["escrow"]))
        self.stat_labels["pos"].configure(text=str(status["positions"]))
        self.stat_labels["orders"].configure(text=str(status["orders"]))
        for name in ("arb", "longshot", "mm"):
            used = status["used"].get(name, 0)
            budget = status["budgets"].get(name, 0)
            self.stat_labels[name].configure(
                text=f"{micro_to_display(used)} / {micro_to_display(budget)}")
        if status["halted"]:
            self.halt_label.configure(
                text="HALTED: " + "; ".join(status["halt_reasons"]))
        else:
            self.halt_label.configure(text="")

    def _update_mode_banner(self) -> None:
        env = str(self.vars["KALSHI_ENV"].get())
        dry = bool(self.vars["DRY_RUN"].get())
        running = self.runner.running
        if env == "prod" and not dry:
            text, bg = "LIVE - REAL MONEY", RED
        elif env == "prod":
            text, bg = "PAPER - real prod data", AMBER
        elif not dry:
            text, bg = "DEMO exchange - live orders (play money)", AMBER
        else:
            text, bg = "PAPER - demo exchange", ACCENT
        text += "  |  RUNNING" if running else "  |  stopped"
        self.mode_label.configure(text=f"  {text}  ", background=bg,
                                  foreground="#0b0e11")

    def _update_buttons(self) -> None:
        if self._stopping and not self.runner.running:
            self._stopping = False
            self.start_btn.configure(text="Start", state="normal")
            if self.runner.error:
                messagebox.showerror("Bot stopped", self.runner.error,
                                     parent=self.root)
        elif not self.runner.running and self.start_btn["text"] == "Stop":
            self.start_btn.configure(text="Start", state="normal")
            if self.runner.error:
                messagebox.showerror("Bot stopped", self.runner.error,
                                     parent=self.root)

    def _redraw_chart(self) -> None:
        points = []
        try:
            state = StateStore(self._db_path())
            points = state.snapshots_since(int(time.time()) - CHART_WINDOW_S)
            state.close()
        except Exception:
            pass
        canvas = self.chart
        canvas.delete("all")
        width = max(canvas.winfo_width(), 50)
        height = max(canvas.winfo_height(), 50)
        if len(points) < 2:
            canvas.create_text(width // 2, height // 2, fill="#76808a",
                               text="equity history appears here once the bot runs")
            return
        ts = [p[0] for p in points]
        eq = [p[1] for p in points]
        lo, hi = min(eq), max(eq)
        if hi == lo:
            hi += 1
        pad = 10
        span_t = max(ts[-1] - ts[0], 1)
        coords = []
        for t, e in points:
            x = pad + (t - ts[0]) / span_t * (width - 2 * pad)
            y = height - pad - (e - lo) / (hi - lo) * (height - 2 * pad)
            coords.extend((x, y))
        color = ACCENT if eq[-1] >= eq[0] else RED
        canvas.create_line(*coords, fill=color, width=2, smooth=True)
        canvas.create_text(width - pad, pad, anchor="ne", fill="#8a949e",
                           text=micro_to_display(hi))
        canvas.create_text(width - pad, height - pad, anchor="se", fill="#8a949e",
                           text=micro_to_display(lo))

    # ----------------------------------------------------------------- exit
    def _on_close(self) -> None:
        if self.runner.running:
            if not messagebox.askokcancel(
                    "Quit", "Stop the bot (cancelling resting orders) and exit?",
                    parent=self.root):
                return
            self.runner.stop()
            self.start_btn.configure(text="Stopping...", state="disabled")
            self._wait_exit(deadline=time.monotonic() + 20)
            return
        self.root.destroy()

    def _wait_exit(self, deadline: float) -> None:
        if self.runner.running and time.monotonic() < deadline:
            self.root.after(200, lambda: self._wait_exit(deadline))
            return
        self.root.destroy()


HELP_TEXT = """Kalshi Bot -- autonomous, risk-managed trading on Kalshi event markets.

QUICK START
 1. Create an account and an API key (demo accounts are free at demo.kalshi.co;
    the production exchange is kalshi.com). Download the private key .pem file.
 2. Settings tab: paste the API key ID, pick the .pem file, Save.
 3. Press Start. The default mode is PAPER on the demo exchange: real market
    data, simulated fills, zero risk. Watch the dashboard and the log.

THE SAFETY LADDER
 paper/demo -> paper/prod data -> demo exchange live -> real money.
 Real money needs all three: Exchange=prod, Paper trading unchecked, and the
 acknowledgement field set to {ack}. The bot will still refuse to start
 without it.

WHAT IT TRADES
 - Event arbitrage: buys/sells complete outcome sets of mutually exclusive
   events when their prices sum away from $1 by more than the fees.
 - Favorite harvesting: buys 90-97c favorites close to resolution when the
   expected value clears fees under a calibration assumption (measure it
   with `kalshi-bot calibrate` in a terminal).
 - Market making: rests post-only quotes in liquid, wide-spread markets.

HOW IT COMPOUNDS
 Every budget and position size is a fraction of current portfolio equity.
 Wins raise the base; losses shrink it. No manual input needed.

RISK CONTROLS
 Per-market/event/series/global exposure caps, quarter-Kelly sizing, a daily
 loss halt, a max-drawdown halt (requires Resume), and the Kill switch above,
 which halts trading and cancels resting orders.

YOUR DATA
 Settings, state and logs live in: {data}

No profit is guaranteed. Trading involves risk of loss. Not financial advice.
"""


def main() -> int:
    ensure_data_dir()
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"Cannot start GUI (no display?): {exc}", file=sys.stderr)
        return 1
    try:
        App(root)
        root.mainloop()
        return 0
    except Exception:
        crash = user_data_dir() / "crash.log"
        try:
            crash.write_text(traceback.format_exc(), encoding="utf-8")
        except OSError:
            pass
        raise


if __name__ == "__main__":
    sys.exit(main())
