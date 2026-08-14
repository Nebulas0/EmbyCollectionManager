# Entry point for Emby Collection Manager
# Starts the Web UI and background sync scheduler

import os
import sys
import time
import logging
import threading
from datetime import datetime, timedelta

from src.config_loader import ConfigLoader
from src.logging_setup import setup_logging

def run_sync_once(config_path="config/config.yaml"):
    """Run a single sync cycle."""
    logger = logging.getLogger("EmbyCollectionManager")
    try:
        from src.app_logic import main as app_main
        # Load config to get sync target
        config = ConfigLoader(yaml_path=config_path)
        sync_target = config.get('SYNC_TARGET', 'auto')

        # Load webui state to get enabled recipes
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
        if enabled_recipes is not None:
            app_logic.RECIPES = [r for r in RECIPES if r.get('name') in enabled_recipes]
            logger.info(f"Filtered to {len(app_logic.RECIPES)} enabled recipes")

        old_argv = sys.argv
        sys.argv = ['app_logic', '--config', config_path, '--targets', sync_target]
        try:
            start_time = datetime.now()
            logger.info(f"Starting collection sync at {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
            app_main()
            logger.info("Collection sync completed successfully")
        finally:
            sys.argv = old_argv
            app_logic.RECIPES = original_recipes
    except Exception as e:
        logger.error(f"Error in sync cycle: {e}")

def sync_scheduler(config_path="config/config.yaml"):
    """Background scheduler that runs sync periodically."""
    logger = logging.getLogger("EmbyCollectionManager")
    logger.info("Starting background sync scheduler")

    # Get interval from webui state
    def get_interval():
        state_path = os.path.join(os.path.dirname(config_path), 'webui_state.json')
        try:
            import json
            with open(state_path, 'r') as f:
                state = json.load(f)
                return state.get('sync_interval_hours', 24)
        except Exception:
            return 24

    while True:
        interval_hours = get_interval()
        start_time = datetime.now()
        run_sync_once(config_path)
        next_run = start_time + timedelta(hours=interval_hours)
        sleep_duration = (next_run - datetime.now()).total_seconds()
        if sleep_duration < 0:
            sleep_duration = 0
        logger.info(f"Next sync in {sleep_duration/3600:.1f} hours")
        time.sleep(sleep_duration)

def main():
    setup_logging()
    logger = logging.getLogger("EmbyCollectionManager")

    # Check if we should run in sync-only mode (no web UI)
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

    # Default: start web UI + background sync scheduler
    logger.info("Starting Emby Collection Manager with Web UI")

    # Start sync scheduler in background thread
    sync_thread = threading.Thread(target=sync_scheduler, daemon=True)
    sync_thread.start()
    logger.info("Background sync scheduler started")

    # Start web UI (blocks)
    from web.app import start_webui
    port = int(os.environ.get('WEBUI_PORT', '8282'))
    start_webui(port=port)

if __name__ == "__main__":
    main()
