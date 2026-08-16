# Entry point for Emby Collection Manager
# Starts the Web UI and background sync scheduler

import os
import sys
import time
import logging
import threading
import io
from datetime import datetime, timedelta
from logging.handlers import MemoryHandler

from src.config_loader import ConfigLoader
from src.logging_setup import setup_logging

# Global log buffer for web UI
log_buffer = io.StringIO()
log_handler = None


class BufferHandler(logging.Handler):
    """Captures log output into a ring buffer for the web UI."""
    def __init__(self, stream, capacity=500):
        super().__init__()
        self.stream = stream
        self.capacity = capacity
        self._lines = []

    def emit(self, record):
        try:
            msg = self.format(record) + '\n'
            self._lines.append(msg)
            if len(self._lines) > self.capacity:
                self._lines = self._lines[-self.capacity:]
            self.stream.write(msg)
        except Exception:
            pass

    def get_lines(self, n=100):
        return ''.join(self._lines[-n:])


buffer_handler = BufferHandler(log_buffer, capacity=500)


def setup_log_capture():
    """Set up log capture for the web UI."""
    global buffer_handler
    formatter = logging.Formatter('[%(asctime)s] %(levelname)s %(name)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    buffer_handler.setFormatter(formatter)
    root_logger = logging.getLogger()
    root_logger.addHandler(buffer_handler)


def get_recent_logs(n=100):
    """Get recent log lines."""
    return buffer_handler.get_lines(n)


def run_sync_once(config_path="config/config.yaml", single_recipe=None):
    """Run a single sync cycle. If single_recipe is set, only sync that recipe."""
    logger = logging.getLogger("EmbyCollectionManager")
    from src.sync_history import SyncHistory
    from src.notifier import Notifier

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    history = SyncHistory(base_dir)

    start_time = datetime.now()
    start_iso = start_time.isoformat()
    logger.info(f"Starting sync at {start_time.strftime('%Y-%m-%d %H:%M:%S')}")

    # Load config for notifier
    try:
        config_loader = ConfigLoader(yaml_path=config_path)
        config = config_loader.get_all()
    except Exception:
        config = {}
    notifier = Notifier(config)

    # Track errors
    error_count = 0
    collections_processed = 0

    try:
        from src.app_logic import main as app_main
        config_loader = ConfigLoader(yaml_path=config_path)
        sync_target = config_loader.get('SYNC_TARGET', 'auto')

        # Load webui state
        state_path = os.path.join(os.path.dirname(config_path), 'webui_state.json')
        import json
        enabled_recipes = None
        try:
            with open(state_path, 'r') as f:
                state = json.load(f)
                enabled_recipes = state.get('enabled_recipes')
        except Exception:
            pass

        # Patch RECIPES if filtering is needed
        import src.app_logic as app_logic
        from src.collection_recipes import RECIPES
        original_recipes = app_logic.RECIPES
        # Clear cancel event for this sync run
        if app_logic._cancel_event is not None:
            app_logic._cancel_event.clear()

        if single_recipe:
            # Single recipe/list mode - sync only one collection
            # Check if it's a built-in recipe
            matching = [r for r in RECIPES if r.get('name') == single_recipe]
            if matching:
                app_logic.RECIPES = matching
                logger.info(f"Single-recipe mode: syncing only '{single_recipe}'")
            else:
                # It's a list collection (MDBList/Trakt), not a built-in recipe
                # Set RECIPES to empty so no built-in recipes run, and pass the
                # single name to app_logic via _single_recipe_mode
                app_logic.RECIPES = []
                logger.info(f"Single-list mode: syncing only list collection '{single_recipe}'")
            # Tell app_logic to only process this one collection
            app_logic._single_recipe_mode = single_recipe
            notifier.notify_sync_start(1)
        elif enabled_recipes is not None:
            app_logic.RECIPES = [r for r in original_recipes if r.get('name') in enabled_recipes]
            logger.info(f"Filtered to {len(app_logic.RECIPES)} enabled recipes")
            notifier.notify_sync_start(len(app_logic.RECIPES))
            # Clear single-recipe mode
            app_logic._single_recipe_mode = None

        old_argv = sys.argv
        sys.argv = ['app_logic', '--config', config_path, '--targets', sync_target]
        was_cancelled = False
        try:
            app_main()
            collections_processed = len(app_logic.RECIPES)
            # Check if the sync was cancelled
            if app_logic._cancel_event is not None and app_logic._cancel_event.is_set():
                was_cancelled = True
        finally:
            sys.argv = old_argv
            app_logic.RECIPES = original_recipes
            app_logic._single_recipe_mode = None
            app_logic._cancel_event = None
            app_logic._progress_callback = None

        duration = str(datetime.now() - start_time).split('.')[0]
        if was_cancelled:
            logger.info(f"Sync cancelled after {duration}")
            history.add_entry({
                'timestamp': start_iso,
                'duration': duration,
                'status': 'cancelled',
                'collections': collections_processed,
                'errors': error_count,
                'single_recipe': single_recipe,
            })
        else:
            logger.info(f"Sync completed successfully in {duration}")
            history.add_entry({
                'timestamp': start_iso,
                'duration': duration,
                'status': 'success',
                'collections': collections_processed,
                'errors': error_count,
                'single_recipe': single_recipe,
            })
            notifier.notify_sync_success(duration, collections_processed, error_count)

    except Exception as e:
        duration = str(datetime.now() - start_time).split('.')[0]
        error_count += 1
        logger.error(f"Error in sync cycle: {e}")
        history.add_entry({
            'timestamp': start_iso,
            'duration': duration,
            'status': 'error',
            'error': str(e),
            'collections': collections_processed,
            'errors': error_count,
            'single_recipe': single_recipe,
        })
        notifier.notify_sync_error(str(e), duration)


def sync_scheduler(config_path="config/config.yaml"):
    """Background scheduler that runs sync periodically."""
    logger = logging.getLogger("EmbyCollectionManager")
    logger.info("Starting background sync scheduler")

    def get_interval():
        state_path = os.path.join(os.path.dirname(config_path), 'webui_state.json')
        try:
            import json
            with open(state_path, 'r') as f:
                state = json.load(f)
                return state.get('sync_interval_hours', 24)
        except Exception:
            return 24

    def is_sync_running():
        """Check if a sync is already running (via web UI state)."""
        try:
            from web.app import sync_state, sync_lock
            with sync_lock:
                return sync_state.get('running', False)
        except Exception:
            return False

    # Track next sync time
    next_sync = datetime.now()
    _update_next_sync(next_sync)

    while True:
        interval_hours = get_interval()
        start_time = datetime.now()
        next_sync = start_time + timedelta(hours=interval_hours)
        _update_next_sync(next_sync)
        # Skip if a sync is already running (e.g. user triggered one from UI)
        if is_sync_running():
            logger.info("Scheduler: sync already running, skipping scheduled run")
        else:
            # Mark sync as running in web UI state so the UI shows correct status
            # and prevents concurrent manual syncs
            try:
                from web.app import sync_state, sync_lock
                with sync_lock:
                    sync_state['running'] = True
                    sync_state['last_status'] = 'running'
                    sync_state['last_run'] = datetime.now().isoformat()
                    sync_state['progress'] = None
            except Exception:
                pass
            try:
                run_sync_once(config_path)
                try:
                    from web.app import sync_state, sync_lock
                    with sync_lock:
                        sync_state['last_status'] = 'success'
                except Exception:
                    pass
            except Exception as e:
                try:
                    from web.app import sync_state, sync_lock
                    with sync_lock:
                        sync_state['last_status'] = 'error'
                        sync_state['last_error'] = str(e)
                except Exception:
                    pass
            finally:
                try:
                    from web.app import sync_state, sync_lock
                    with sync_lock:
                        sync_state['running'] = False
                        sync_state['progress'] = None
                except Exception:
                    pass
        sleep_duration = (next_sync - datetime.now()).total_seconds()
        if sleep_duration < 0:
            sleep_duration = 0
        logger.info(f"Next sync in {sleep_duration/3600:.1f} hours (at {next_sync.strftime('%Y-%m-%d %H:%M:%S')})")
        time.sleep(sleep_duration)


_next_sync_time = None


def _update_next_sync(dt):
    global _next_sync_time
    _next_sync_time = dt


def get_next_sync_time():
    return _next_sync_time


def main():
    setup_logging()
    setup_log_capture()
    logger = logging.getLogger("EmbyCollectionManager")

    sync_only = os.getenv('SYNC_ONLY', 'false').lower() == 'true'
    run_once = os.getenv('RUN_ONCE', 'false').lower() == 'true'

    if run_once:
        logger.info("Running in one-time sync mode")
        run_sync_once()
        return

    if sync_only:
        logger.info("Running in sync-only mode (no web UI)")
        sync_scheduler()
        return

    logger.info("Starting Emby Collection Manager with Web UI")

    # Start sync scheduler in background thread
    sync_thread = threading.Thread(target=sync_scheduler, daemon=True)
    sync_thread.start()
    logger.info("Background sync scheduler started")

    # Start web UI (blocks)
    from web.app import start_webui, set_log_provider, set_sync_functions
    set_log_provider(get_recent_logs)
    set_sync_functions(run_sync_once, get_next_sync_time)
    port = int(os.environ.get('WEBUI_PORT', '8282'))
    start_webui(port=port)


if __name__ == "__main__":
    main()
