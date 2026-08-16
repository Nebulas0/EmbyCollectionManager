"""
Web UI for Emby Collection Manager
Provides a Flask-based web interface to manage all configuration,
toggle collection recipes, select libraries, and trigger syncs.
"""

import os
import sys
import json
import yaml
import threading
import time
import logging
import re
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, jsonify, redirect, url_for, Response, send_file

# Add parent directory to path so we can import src modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config_loader import ConfigLoader
from src.collection_recipes import RECIPES, CATEGORY_CONFIG

logger = logging.getLogger("WebUI")

# Provider hooks set by main.py
_log_provider = None
_sync_function = None
_next_sync_provider = None

def set_log_provider(fn):
    global _log_provider
    _log_provider = fn

def set_sync_functions(sync_fn, next_sync_fn):
    global _sync_function, _next_sync_provider
    _sync_function = sync_fn
    _next_sync_provider = next_sync_fn

app = Flask(__name__)

# Paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, 'config', 'config.yaml')
STATE_PATH = os.path.join(BASE_DIR, 'config', 'webui_state.json')

# Sync state tracking
sync_lock = threading.Lock()
sync_cancel = threading.Event()
sync_state = {
    'running': False,
    'last_run': None,
    'last_status': None,
    'last_error': None,
    'thread': None,
    'progress': None,  # {'current': 'Collection Name', 'index': 1, 'total': 5}
}


def load_config():
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return {}


def save_config(config):
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)
        return True
    except Exception as e:
        logger.error(f"Failed to save config: {e}")
        return False


def load_state():
    try:
        with open(STATE_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save state: {e}")


def get_enabled_recipes():
    state = load_state()
    enabled = state.get('enabled_recipes', None)
    if enabled is None:
        return [r['name'] for r in RECIPES if 'name' in r]
    return enabled


def get_filtered_recipes():
    enabled_set = set(get_enabled_recipes())
    result = []
    for recipe in RECIPES:
        if 'name' not in recipe:
            continue
        r = dict(recipe)
        r['enabled'] = recipe['name'] in enabled_set
        cat_id = recipe.get('category_id', 0)
        r['category_name'] = CATEGORY_CONFIG.get(cat_id, {}).get('name', 'Unknown')
        result.append(r)
    return result


def get_emby_libraries():
    config = load_config()
    emby_cfg = config.get('emby', {})
    if not emby_cfg.get('server_url') or not emby_cfg.get('api_key'):
        return []
    try:
        from src.emby_client import EmbyClient
        emby = EmbyClient(
            server_url=emby_cfg['server_url'],
            api_key=emby_cfg['api_key'],
            user_id=emby_cfg.get('user_id', ''),
            config=config
        )
        return emby.get_libraries()
    except Exception as e:
        logger.error(f"Failed to get Emby libraries: {e}")
        return []


def run_sync_background():
    with sync_lock:
        if sync_state['running']:
            return
        sync_state['running'] = True
    sync_cancel.clear()
    sync_state['last_error'] = None
    sync_state['last_status'] = 'running'
    sync_state['last_run'] = datetime.now().isoformat()
    sync_state['progress'] = None
    try:
        from src.app_logic import main as app_main
        import src.app_logic as app_logic_module
        # Pass the cancel event and progress callback to app_logic
        app_logic_module._cancel_event = sync_cancel
        app_logic_module._progress_callback = _update_progress
        state = load_state()
        enabled = state.get('enabled_recipes', None)
        original_recipes = app_logic_module.RECIPES
        # Clear single-recipe mode for full syncs
        app_logic_module._single_recipe_mode = None
        if enabled is not None:
            app_logic_module.RECIPES = [r for r in original_recipes if r.get('name') in enabled]
        old_argv = sys.argv
        sys.argv = ['app_logic', '--config', CONFIG_PATH, '--targets', 'auto']
        try:
            app_main()
        finally:
            sys.argv = old_argv
            app_logic_module.RECIPES = original_recipes
            app_logic_module._single_recipe_mode = None
            app_logic_module._cancel_event = None
            app_logic_module._progress_callback = None
        if sync_cancel.is_set():
            sync_state['last_status'] = 'cancelled'
        else:
            sync_state['last_status'] = 'success'
    except Exception as e:
        sync_state['last_status'] = 'error'
        sync_state['last_error'] = str(e)
        logger.error(f"Sync error: {e}")
    finally:
        with sync_lock:
            sync_state['running'] = False
            sync_state['progress'] = None

def _update_progress(current, index, total):
    """Update sync progress state (called from app_logic)."""
    sync_state['progress'] = {'current': current, 'index': index, 'total': total}


# === Routes ===

@app.route('/health')
def health_check():
    """Health check endpoint for Docker/Saltbox."""
    return jsonify({'status': 'ok', 'running': sync_state['running']})

@app.route('/')
def index():
    return redirect(url_for('dashboard'))


@app.route('/dashboard')
def dashboard():
    config = load_config()
    state = load_state()
    recipes = get_filtered_recipes()
    enabled_count = sum(1 for r in recipes if r['enabled'])
    total_count = len(recipes)
    return render_template('dashboard.html', config=config, state=state,
                           sync_state=sync_state, enabled_count=enabled_count, total_count=total_count)


def _safe_int(val, default=0):
    """Safely parse an integer from a form value."""
    try:
        return int(val) if val else default
    except (ValueError, TypeError):
        return default

def _safe_float(val, default=0.5):
    """Safely parse a float from a form value."""
    try:
        return float(val) if val else default
    except (ValueError, TypeError):
        return default

_VALID_IMAGE_TYPES = ('Primary', 'Backdrop', 'Logo', 'Thumb', 'Banner', 'Art', 'Disc')

def _valid_image_type(image_type):
    """Validate image_type to prevent path traversal."""
    return image_type if image_type in _VALID_IMAGE_TYPES else None

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        try:
            config = load_config()
            config.setdefault('tmdb', {})['api_key'] = request.form.get('tmdb_api_key', '')
            config.setdefault('emby', {})['server_url'] = request.form.get('emby_server_url', '')
            config.setdefault('emby', {})['api_key'] = request.form.get('emby_api_key', '')
            config.setdefault('emby', {})['user_id'] = request.form.get('emby_user_id', '')
            config.setdefault('trakt', {})['client_id'] = request.form.get('trakt_client_id', '')
            config.setdefault('trakt', {})['client_secret'] = request.form.get('trakt_client_secret', '')
            config.setdefault('trakt', {})['access_token'] = request.form.get('trakt_access_token', '')
            config.setdefault('trakt', {})['username'] = request.form.get('trakt_username', 'me')
            config.setdefault('mdblist', {})['api_key'] = request.form.get('mdblist_api_key', '')
            config.setdefault('traktlists', {})['enabled'] = request.form.get('traktlists_enabled') == 'on'
            config['traktlists']['directory'] = request.form.get('traktlists_directory', 'traktlists')
            config['traktlists']['max_items_per_collection'] = _safe_int(request.form.get('traktlists_max_items', '0'), 0)
            config.setdefault('mdblists', {})['enabled'] = request.form.get('mdblists_enabled') == 'on'
            config['mdblists']['directory'] = request.form.get('mdblists_directory', 'mdblists')
            config['mdblists']['max_items_per_collection'] = _safe_int(request.form.get('mdblists_max_items', '0'), 0)
            config.setdefault('poster_settings', {})['enable_custom_posters'] = request.form.get('poster_enable') == 'on'
            config['poster_settings']['template_name'] = request.form.get('poster_template', 'default.png')
            config['poster_settings']['text_position'] = _safe_float(request.form.get('poster_text_position', '0.5'), 0.5)
            config['poster_settings']['text_color'] = [
                _safe_int(request.form.get('text_color_r', 255), 255),
                _safe_int(request.form.get('text_color_g', 255), 255),
                _safe_int(request.form.get('text_color_b', 255), 255)
            ]
            config['poster_settings']['bg_color'] = [
                _safe_int(request.form.get('bg_color_r', 0), 0),
                _safe_int(request.form.get('bg_color_g', 0), 0),
                _safe_int(request.form.get('bg_color_b', 0), 0),
                _safe_int(request.form.get('bg_color_a', 128), 128)
            ]
            state = load_state()
            state['sync_interval_hours'] = _safe_int(request.form.get('sync_interval', '24'), 24)
            save_state(state)
            if save_config(config):
                return render_template('settings.html', config=config, state=state, saved=True)
            return render_template('settings.html', config=config, state=state, error="Failed to save config")
        except Exception as e:
            logger.error(f"Error saving settings: {e}")
            config = load_config()
            state = load_state()
            return render_template('settings.html', config=config, state=state, error=f"Error saving settings: {e}")
    config = load_config()
    state = load_state()
    return render_template('settings.html', config=config, state=state)


@app.route('/libraries', methods=['GET', 'POST'])
def libraries():
    if request.method == 'POST':
        selected = request.form.getlist('library_ids')
        config = load_config()
        config.setdefault('emby', {})['library_ids'] = selected
        save_config(config)
        libs = get_emby_libraries()
        return render_template('libraries.html', libraries=libs, selected=selected, saved=True)
    config = load_config()
    selected = config.get('emby', {}).get('library_ids', [])
    libs = get_emby_libraries()
    return render_template('libraries.html', libraries=libs, selected=selected)


@app.route('/collections')
def collections():
    recipes = get_filtered_recipes()
    categories = {}
    for r in recipes:
        cat_id = r.get('category_id', 0)
        cat_name = r.get('category_name', 'Unknown')
        if cat_id not in categories:
            categories[cat_id] = {'name': cat_name, 'recipes': []}
        categories[cat_id]['recipes'].append(r)
    categories = sorted(categories.items())
    return render_template('collections.html', categories=categories)


@app.route('/api/toggle_recipe', methods=['POST'])
def toggle_recipe():
    data = request.json
    name = data.get('name')
    enabled = data.get('enabled')
    if not name:
        return jsonify({'error': 'No recipe name'}), 400
    state = load_state()
    cur = set(get_enabled_recipes())
    if enabled:
        cur.add(name)
    else:
        cur.discard(name)
    state['enabled_recipes'] = list(cur)
    save_state(state)
    return jsonify({'success': True, 'enabled_count': len(cur)})


@app.route('/api/toggle_all', methods=['POST'])
def toggle_all():
    data = request.json
    enable = data.get('enable', True)
    state = load_state()
    if enable:
        state['enabled_recipes'] = [r['name'] for r in RECIPES if 'name' in r]
    else:
        state['enabled_recipes'] = []
    save_state(state)
    return jsonify({'success': True, 'enabled_count': len(state['enabled_recipes'])})


@app.route('/api/toggle_category', methods=['POST'])
def toggle_category():
    data = request.json
    cat_id = data.get('category_id')
    enabled = data.get('enabled')
    if cat_id is None:
        return jsonify({'error': 'No category_id'}), 400
    state = load_state()
    cur = set(get_enabled_recipes())
    for r in RECIPES:
        if r.get('category_id') == cat_id and 'name' in r:
            if enabled:
                cur.add(r['name'])
            else:
                cur.discard(r['name'])
    state['enabled_recipes'] = list(cur)
    save_state(state)
    return jsonify({'success': True, 'enabled_count': len(cur)})


@app.route('/api/toggle_list', methods=['POST'])
def toggle_list():
    """Toggle enable/disable for an MDBList or Trakt list collection."""
    data = request.json
    name = data.get('name')
    enabled = data.get('enabled')
    if not name:
        return jsonify({'error': 'No name'}), 400
    state = load_state()
    cur = set(get_enabled_recipes())
    if enabled:
        cur.add(name)
    else:
        cur.discard(name)
    state['enabled_recipes'] = list(cur)
    save_state(state)
    return jsonify({'success': True, 'enabled_count': len(cur)})


@app.route('/api/toggle_all_lists', methods=['POST'])
def toggle_all_lists():
    """Enable/disable all MDBList or Trakt list collections."""
    data = request.json
    list_type = data.get('type', 'mdblists')
    enable = data.get('enable', True)
    config = load_config()
    d = config.get(list_type, {}).get('directory', list_type)
    p = os.path.join(BASE_DIR, d)
    state = load_state()
    cur = set(get_enabled_recipes())
    if os.path.isdir(p):
        for f in sorted(os.listdir(p)):
            if f.endswith(('.txt', '.yaml', '.yml')):
                col_name = os.path.splitext(f)[0]
                try:
                    import yaml as _yaml
                    with open(os.path.join(p, f), 'r', encoding='utf-8') as fh:
                        parsed = _yaml.safe_load(fh.read())
                    if isinstance(parsed, dict) and parsed.get('collection_name'):
                        col_name = parsed['collection_name']
                except Exception:
                    pass
                if enable:
                    cur.add(col_name)
                else:
                    cur.discard(col_name)
    state['enabled_recipes'] = list(cur)
    save_state(state)
    return jsonify({'success': True, 'enabled_count': len(cur)})


@app.route('/api/sync_single_list', methods=['POST'])
def sync_single_list():
    """Sync a single MDBList or Trakt list collection by name."""
    data = request.json
    name = data.get('name')
    if not name:
        return jsonify({'error': 'No name'}), 400
    with sync_lock:
        if sync_state['running']:
            return jsonify({'error': 'A sync is already running'}), 409
        sync_state['running'] = True
    sync_cancel.clear()

    def run_single():
        sync_state['last_error'] = None
        sync_state['last_status'] = 'running'
        sync_state['last_run'] = datetime.now().isoformat()
        sync_state['progress'] = None
        # Set cancel event and progress callback on app_logic so
        # single-list sync also supports cancel and progress reporting
        try:
            import src.app_logic as app_logic_module
            app_logic_module._cancel_event = sync_cancel
            app_logic_module._progress_callback = _update_progress
        except Exception:
            pass
        try:
            if _sync_function:
                _sync_function(single_recipe=name)
            else:
                run_sync_background()
            if sync_cancel.is_set():
                sync_state['last_status'] = 'cancelled'
            else:
                sync_state['last_status'] = 'success'
        except Exception as e:
            sync_state['last_status'] = 'error'
            sync_state['last_error'] = str(e)
        finally:
            try:
                import src.app_logic as app_logic_module
                app_logic_module._cancel_event = None
                app_logic_module._progress_callback = None
            except Exception:
                pass
            with sync_lock:
                sync_state['running'] = False
                sync_state['progress'] = None

    thread = threading.Thread(target=run_single, daemon=True)
    thread.start()
    return jsonify({'success': True, 'message': f'Syncing {name}'})


@app.route('/api/list_detail/<path:list_name>')
def list_detail(list_name):
    """Get detail for an MDBList/Trakt list collection: file content, override, libraries."""
    from src.recipe_override import RecipeOverrideManager
    config = load_config()
    # Determine which directory to look in based on the request referrer or a query param
    list_type = request.args.get('type', 'mdblists')
    d = config.get(list_type, {}).get('directory', list_type)
    p = os.path.join(BASE_DIR, d)
    # Find the file - match by filename or collection_name
    file_content = None
    file_name = None
    collection_name = list_name
    if os.path.isdir(p):
        for f in sorted(os.listdir(p)):
            if f.endswith(('.txt', '.yaml', '.yml')):
                fp = os.path.join(p, f)
                with open(fp, 'r', encoding='utf-8') as fh:
                    c = fh.read()
                # Check if filename matches or collection_name matches
                col_name = os.path.splitext(f)[0]
                try:
                    import yaml as _yaml
                    parsed = _yaml.safe_load(c)
                    if isinstance(parsed, dict) and parsed.get('collection_name'):
                        col_name = parsed['collection_name']
                except Exception:
                    pass
                if f == list_name or col_name == list_name:
                    file_content = c
                    file_name = f
                    collection_name = col_name
                    break
    # Get override
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    override_mgr = RecipeOverrideManager(base_dir)
    override = override_mgr.get_override(collection_name)
    # Parse file content for display
    parsed = None
    items = []
    library_ids_from_file = []
    if file_content:
        try:
            import yaml as _yaml
            parsed = _yaml.safe_load(file_content)
            if isinstance(parsed, dict):
                items = parsed.get('items', [])
                library_ids_from_file = parsed.get('library_ids', [])
        except Exception:
            # Plain text file - one item per line
            items = [line.strip() for line in file_content.splitlines() if line.strip() and not line.startswith('#')]
    return jsonify({
        'collection_name': collection_name,
        'file_name': file_name,
        'file_content': file_content,
        'items': items,
        'library_ids_from_file': library_ids_from_file,
        'override': override,
        'list_type': list_type,
    })


@app.route('/api/list_override', methods=['POST'])
def save_list_override():
    """Save override for an MDBList/Trakt list collection."""
    from src.recipe_override import RecipeOverrideManager
    data = request.json
    name = data.get('name')
    override = data.get('override', {})
    if not name:
        return jsonify({'error': 'No name'}), 400
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    mgr = RecipeOverrideManager(base_dir)
    mgr.set_override(name, override)
    return jsonify({'success': True})


@app.route('/api/list_override_delete', methods=['POST'])
def delete_list_override():
    """Delete override for an MDBList/Trakt list collection."""
    from src.recipe_override import RecipeOverrideManager
    data = request.json
    name = data.get('name')
    if not name:
        return jsonify({'error': 'No name'}), 400
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    mgr = RecipeOverrideManager(base_dir)
    mgr.delete_override(name)
    return jsonify({'success': True})


@app.route('/api/create_from_url', methods=['POST'])
def create_from_url():
    """Create a new MDBList or Trakt list file from a URL.
    Fetches the list name from the API and creates a YAML file."""
    data = request.json
    url = data.get('url', '').strip()
    list_type = data.get('type', 'mdblists')
    custom_name = data.get('custom_name', '').strip()
    library_ids = data.get('library_ids', [])

    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    config = load_config()
    d = config.get(list_type, {}).get('directory', list_type)
    p = os.path.join(BASE_DIR, d)
    os.makedirs(p, exist_ok=True)

    # Determine collection name and file content based on type
    if list_type == 'mdblists':
        # Try to fetch list info from MDBList API
        mdblist_cfg = config.get('mdblist', {})
        api_key = mdblist_cfg.get('api_key', '')
        if not api_key:
            return jsonify({'error': 'MDBList API key not configured'}), 400
        try:
            from src.mdblist_client import MDBListClient
            client = MDBListClient(api_key=api_key)
            list_id = client._extract_list_id_from_url(url)
            if not list_id:
                return jsonify({'error': 'Could not extract list ID from URL'}), 400
            # Fetch a few items to get the list name
            items = client.get_list_items(url, limit=1)
            # Try to get list info
            list_info = client._make_request(f"lists/{list_id}")
            list_name = ''
            if list_info and isinstance(list_info, dict):
                list_name = list_info.get('name', '')
            if not list_name:
                list_name = list_id.split('/')[-1].replace('-', ' ').title()
            collection_name = custom_name or list_name
        except Exception as e:
            return jsonify({'error': f'Failed to fetch MDBList info: {e}'}), 500

        # Create YAML file
        safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', collection_name)
        if not safe_name or safe_name == '_':
            safe_name = 'untitled'
        filename = f"{safe_name}.yaml"
        filepath = os.path.join(p, filename)
        # Don't overwrite existing files
        if os.path.exists(filepath):
            return jsonify({'error': f'File already exists: {filename}'}), 409
        yaml_content = f"collection_name: {collection_name}\n"
        if library_ids:
            yaml_content += "library_ids:\n"
            for lid in library_ids:
                yaml_content += f"  - '{lid}'\n"
        else:
            yaml_content += "library_ids: []\n"
        yaml_content += f"items:\n  - {url}\n"
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(yaml_content)
        return jsonify({'success': True, 'filename': filename, 'collection_name': collection_name})

    elif list_type == 'traktlists':
        # Parse Trakt URL: https://trakt.tv/users/username/lists/list-name
        import re
        m = re.match(r'https://trakt\.tv/users/([^/]+)/lists/([^/?]+)', url)
        if not m:
            return jsonify({'error': 'Invalid Trakt URL. Expected: https://trakt.tv/users/username/lists/list-name'}), 400
        username = m.group(1)
        list_slug = m.group(2)
        # Try to fetch list info from Trakt
        trakt_cfg = config.get('trakt', {})
        client_id = trakt_cfg.get('client_id', '')
        list_name = ''
        if client_id and client_id != 'YOUR TRAKT CLIENT ID':
            try:
                from src.trakt_client import TraktClient
                trakt = TraktClient(client_id=client_id, client_secret=trakt_cfg.get('client_secret', ''), access_token=trakt_cfg.get('access_token', ''))
                list_info = trakt.get_list_info(username, list_slug)
                if list_info:
                    list_name = list_info.get('name', '')
            except Exception:
                pass
        if not list_name:
            list_name = list_slug.replace('-', ' ').title()
        collection_name = custom_name or list_name

        safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', collection_name)
        if not safe_name or safe_name == '_':
            safe_name = 'untitled'
        filename = f"{safe_name}.yaml"
        filepath = os.path.join(p, filename)
        if os.path.exists(filepath):
            return jsonify({'error': f'File already exists: {filename}'}), 409
        yaml_content = f"collection_name: {collection_name}\n"
        if library_ids:
            yaml_content += "library_ids:\n"
            for lid in library_ids:
                yaml_content += f"  - '{lid}'\n"
        else:
            yaml_content += "library_ids: []\n"
        yaml_content += f"items:\n  - {url}\n"
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(yaml_content)
        return jsonify({'success': True, 'filename': filename, 'collection_name': collection_name})

    else:
        return jsonify({'error': 'Invalid type'}), 400


@app.route('/api/cleanup_collections', methods=['POST'])
def cleanup_collections():
    """Remove Emby collections that are not in the enabled recipes/lists."""
    data = request.json or {}
    dry_run = data.get('dry_run', False)
    config = load_config()
    emby_cfg = config.get('emby', {})
    if not emby_cfg.get('server_url') or not emby_cfg.get('api_key'):
        return jsonify({'error': 'Emby not configured'}), 400

    from src.emby_client import EmbyClient
    emby = EmbyClient(
        server_url=emby_cfg['server_url'],
        api_key=emby_cfg['api_key'],
        user_id=emby_cfg.get('user_id', ''),
        config={}
    )

    # Get all collections from Emby
    params = {'IncludeItemTypes': 'BoxSet', 'Recursive': 'true', 'Fields': 'Name'}
    endpoint = f"/Users/{emby.user_id}/Items"
    data_resp = emby._make_api_request('GET', endpoint, params=params)
    emby_collections = {}
    if data_resp and 'Items' in data_resp:
        for item in data_resp['Items']:
            emby_collections[item['Name']] = item['Id']

    # Get all managed collection names (enabled recipes + enabled lists)
    enabled_set = set(get_enabled_recipes())
    # Also get ALL recipe names (even disabled ones, since they're managed)
    from src.collection_recipes import RECIPES
    all_managed = set(r.get('name', '') for r in RECIPES)
    # Get override manager once (not inside the loop)
    from src.recipe_override import RecipeOverrideManager
    mgr = RecipeOverrideManager(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    # Add all MDBList and Trakt collection names + custom_name overrides
    for list_type in ['mdblists', 'traktlists']:
        d = config.get(list_type, {}).get('directory', list_type)
        p = os.path.join(BASE_DIR, d)
        if os.path.isdir(p):
            import yaml as _yaml
            for f in sorted(os.listdir(p)):
                if f.endswith(('.txt', '.yaml', '.yml')):
                    col_name = os.path.splitext(f)[0]
                    try:
                        with open(os.path.join(p, f), 'r', encoding='utf-8') as fh:
                            parsed = _yaml.safe_load(fh.read())
                        if isinstance(parsed, dict) and parsed.get('collection_name'):
                            col_name = parsed['collection_name']
                    except Exception:
                        pass
                    all_managed.add(col_name)
                    # Check for custom_name override
                    ov = mgr.get_override(col_name)
                    if ov.get('custom_name'):
                        all_managed.add(ov['custom_name'])
    # Also check overrides for built-in recipes
    all_overrides = mgr.get_all_overrides()
    for name, ov in all_overrides.items():
        if ov.get('custom_name'):
            all_managed.add(ov['custom_name'])

    # Find collections in Emby that are not managed
    to_remove = {}
    for name, cid in emby_collections.items():
        if name not in all_managed:
            to_remove[name] = cid

    if dry_run:
        return jsonify({'success': True, 'dry_run': True, 'would_remove': to_remove, 'count': len(to_remove)})

    removed = {}
    errors = {}
    for name, cid in to_remove.items():
        try:
            # Delete the collection from Emby
            del_url = f"{emby.server_url}/Items/{cid}?api_key={emby.api_key}"
            resp = emby.session.delete(del_url, timeout=15)
            if resp.status_code in (200, 204):
                removed[name] = cid
            else:
                errors[name] = f"HTTP {resp.status_code}"
        except Exception as e:
            errors[name] = str(e)

    return jsonify({'success': True, 'removed': removed, 'errors': errors, 'count': len(removed)})


@app.route('/api/sync', methods=['POST'])
def trigger_sync():
    with sync_lock:
        if sync_state['running']:
            return jsonify({'error': 'Sync already running'}), 409
    thread = threading.Thread(target=run_sync_background, daemon=True)
    sync_state['thread'] = thread
    thread.start()
    return jsonify({'success': True, 'message': 'Sync started'})


@app.route('/api/sync_cancel', methods=['POST'])
def cancel_sync():
    """Cancel a running sync."""
    if not sync_state['running']:
        return jsonify({'error': 'No sync running'}), 400
    sync_cancel.set()
    logger.info("Sync cancellation requested")
    return jsonify({'success': True, 'message': 'Cancellation requested'})

@app.route('/api/sync_status')
def sync_status():
    return jsonify({
        'running': sync_state['running'],
        'last_run': sync_state['last_run'],
        'last_status': sync_state['last_status'],
        'last_error': sync_state['last_error'],
        'progress': sync_state.get('progress'),
    })


@app.route('/api/test_emby', methods=['POST'])
def test_emby():
    data = request.json
    try:
        from src.emby_client import EmbyClient
        emby = EmbyClient(server_url=data.get('server_url', ''), api_key=data.get('api_key', ''), user_id=data.get('user_id', ''), config={})
        libs = emby.get_libraries()
        if not libs:
            return jsonify({'success': False, 'error': 'No libraries returned. Check API key and server URL.'}), 200
        return jsonify({'success': True, 'libraries': libs, 'count': len(libs)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/test_tmdb', methods=['POST'])
def test_tmdb():
    data = request.json
    try:
        from src.tmdb_client import TmdbClient
        tmdb = TmdbClient(api_key=data.get('api_key', ''))
        movies = tmdb.discover_movies({'sort_by': 'popularity.desc'}, page_limit=1)
        if not movies:
            return jsonify({'success': False, 'error': 'TMDb returned 0 movies. Check API key (needs v3 key, not v4 JWT token).'}), 200
        return jsonify({'success': True, 'movie_count': len(movies), 'sample': movies[0]['title'] if movies else None})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/test_trakt', methods=['POST'])
def test_trakt():
    data = request.json
    client_id = data.get('client_id', '').strip()
    if not client_id:
        return jsonify({'success': False, 'error': 'No Trakt Client ID provided'}), 200
    try:
        from src.trakt_client import TraktClient
        trakt = TraktClient(client_id=client_id)
        trending = trakt.get_trending_lists(1)
        if trending is None:
            return jsonify({'success': False, 'error': 'Trakt API returned an error. Check your Client ID.'}), 200
        return jsonify({'success': True, 'list_count': len(trending)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/test_mdblist', methods=['POST'])
def test_mdblist():
    data = request.json
    api_key = data.get('api_key', '').strip()
    if not api_key:
        return jsonify({'success': False, 'error': 'No MDBList API key provided'}), 200
    try:
        from src.mdblist_client import MDBListClient
        client = MDBListClient(api_key=api_key)
        result = client._make_request('/user/')
        if result is None:
            return jsonify({'success': False, 'error': 'MDBList API returned an error. Check your API key.'}), 200
        return jsonify({'success': True, 'data': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def _safe_filename(name):
    if not name:
        return None
    # Allow .txt, .yaml, .yml
    valid_exts = ('.txt', '.yaml', '.yml')
    if not any(name.endswith(ext) for ext in valid_exts):
        return None
    if not re.match(r'^[\w\-\s]+\.(txt|yaml|yml)$', name):
        return None
    return name


@app.route('/traktlists')
def traktlists():
    config = load_config()
    d = config.get('traktlists', {}).get('directory', 'traktlists')
    p = os.path.join(BASE_DIR, d)
    enabled_set = set(get_enabled_recipes())
    files = []
    if os.path.isdir(p):
        for f in sorted(os.listdir(p)):
            if f.endswith(('.txt', '.yaml', '.yml')):
                with open(os.path.join(p, f), 'r', encoding='utf-8') as fh:
                    c = fh.read()
                # Derive collection name from file
                col_name = os.path.splitext(f)[0]
                try:
                    import yaml as _yaml
                    parsed = _yaml.safe_load(c)
                    if isinstance(parsed, dict) and parsed.get('collection_name'):
                        col_name = parsed['collection_name']
                except Exception:
                    pass
                files.append({'name': f, 'content': c, 'lines': len(c.splitlines()),
                              'collection_name': col_name, 'enabled': col_name in enabled_set})
    return render_template('traktlists.html', files=files, directory=d, enabled_count=len([f for f in files if f['enabled']]))


@app.route('/traktlists/save', methods=['POST'])
def save_traktlist():
    data = request.json
    fn = _safe_filename(data.get('name', ''))
    if not fn:
        return jsonify({'error': 'Invalid filename'}), 400
    config = load_config()
    d = config.get('traktlists', {}).get('directory', 'traktlists')
    p = os.path.join(BASE_DIR, d)
    os.makedirs(p, exist_ok=True)
    # Check if collection_name changed and clean up orphaned state
    fp = os.path.join(p, fn)
    if os.path.exists(fp):
        old_col_name = os.path.splitext(fn)[0]
        try:
            import yaml as _yaml
            with open(fp, 'r', encoding='utf-8') as fh:
                old_parsed = _yaml.safe_load(fh.read())
            if isinstance(old_parsed, dict) and old_parsed.get('collection_name'):
                old_col_name = old_parsed['collection_name']
        except Exception:
            pass
        # Parse new content to get new collection_name
        new_col_name = os.path.splitext(fn)[0]
        try:
            import yaml as _yaml
            new_parsed = _yaml.safe_load(data.get('content', ''))
            if isinstance(new_parsed, dict) and new_parsed.get('collection_name'):
                new_col_name = new_parsed['collection_name']
        except Exception:
            pass
        # If collection_name changed, clean up old state
        if old_col_name != new_col_name:
            state = load_state()
            cur = set(state.get('enabled_recipes', []))
            if old_col_name in cur:
                cur.discard(old_col_name)
                state['enabled_recipes'] = list(cur)
                save_state(state)
            try:
                from src.recipe_override import RecipeOverrideManager
                mgr = RecipeOverrideManager(BASE_DIR)
                mgr.delete_override(old_col_name)
            except Exception:
                pass
    with open(fp, 'w', encoding='utf-8') as f:
        f.write(data.get('content', ''))
    return jsonify({'success': True})


@app.route('/traktlists/delete', methods=['POST'])
def delete_traktlist():
    data = request.json
    fn = _safe_filename(data.get('name', ''))
    if not fn:
        return jsonify({'error': 'Invalid filename'}), 400
    config = load_config()
    d = config.get('traktlists', {}).get('directory', 'traktlists')
    p = os.path.join(BASE_DIR, d)
    fp = os.path.join(p, fn)
    if os.path.exists(fp):
        # Derive collection name before deleting
        col_name = os.path.splitext(fn)[0]
        try:
            import yaml as _yaml
            with open(fp, 'r', encoding='utf-8') as fh:
                parsed = _yaml.safe_load(fh.read())
            if isinstance(parsed, dict) and parsed.get('collection_name'):
                col_name = parsed['collection_name']
        except Exception:
            pass
        os.remove(fp)
        # Clean up enabled state
        state = load_state()
        cur = set(state.get('enabled_recipes', []))
        cur.discard(col_name)
        state['enabled_recipes'] = list(cur)
        save_state(state)
        # Clean up override
        try:
            from src.recipe_override import RecipeOverrideManager
            mgr = RecipeOverrideManager(BASE_DIR)
            mgr.delete_override(col_name)
        except Exception:
            pass
        # Clean up list metadata
        try:
            from src.list_metadata import ListMetadataManager
            meta_mgr = ListMetadataManager(BASE_DIR)
            meta_mgr.delete_list_config('traktlists', fn)
        except Exception:
            pass
        return jsonify({'success': True})
    return jsonify({'error': 'File not found'}), 404


@app.route('/mdblists')
def mdblists():
    config = load_config()
    d = config.get('mdblists', {}).get('directory', 'mdblists')
    p = os.path.join(BASE_DIR, d)
    enabled_set = set(get_enabled_recipes())
    files = []
    if os.path.isdir(p):
        for f in sorted(os.listdir(p)):
            if f.endswith(('.txt', '.yaml', '.yml')):
                with open(os.path.join(p, f), 'r', encoding='utf-8') as fh:
                    c = fh.read()
                # Derive collection name from file
                col_name = os.path.splitext(f)[0]
                try:
                    import yaml as _yaml
                    parsed = _yaml.safe_load(c)
                    if isinstance(parsed, dict) and parsed.get('collection_name'):
                        col_name = parsed['collection_name']
                except Exception:
                    pass
                files.append({'name': f, 'content': c, 'lines': len(c.splitlines()),
                              'collection_name': col_name, 'enabled': col_name in enabled_set})
    return render_template('mdblists.html', files=files, directory=d, enabled_count=len([f for f in files if f['enabled']]))


@app.route('/mdblists/save', methods=['POST'])
def save_mdblist():
    data = request.json
    fn = _safe_filename(data.get('name', ''))
    if not fn:
        return jsonify({'error': 'Invalid filename'}), 400
    config = load_config()
    d = config.get('mdblists', {}).get('directory', 'mdblists')
    p = os.path.join(BASE_DIR, d)
    os.makedirs(p, exist_ok=True)
    # Check if collection_name changed and clean up orphaned state
    fp = os.path.join(p, fn)
    if os.path.exists(fp):
        old_col_name = os.path.splitext(fn)[0]
        try:
            import yaml as _yaml
            with open(fp, 'r', encoding='utf-8') as fh:
                old_parsed = _yaml.safe_load(fh.read())
            if isinstance(old_parsed, dict) and old_parsed.get('collection_name'):
                old_col_name = old_parsed['collection_name']
        except Exception:
            pass
        # Parse new content to get new collection_name
        new_col_name = os.path.splitext(fn)[0]
        try:
            import yaml as _yaml
            new_parsed = _yaml.safe_load(data.get('content', ''))
            if isinstance(new_parsed, dict) and new_parsed.get('collection_name'):
                new_col_name = new_parsed['collection_name']
        except Exception:
            pass
        # If collection_name changed, clean up old state
        if old_col_name != new_col_name:
            state = load_state()
            cur = set(state.get('enabled_recipes', []))
            if old_col_name in cur:
                cur.discard(old_col_name)
                state['enabled_recipes'] = list(cur)
                save_state(state)
            try:
                from src.recipe_override import RecipeOverrideManager
                mgr = RecipeOverrideManager(BASE_DIR)
                mgr.delete_override(old_col_name)
            except Exception:
                pass
    with open(fp, 'w', encoding='utf-8') as f:
        f.write(data.get('content', ''))
    return jsonify({'success': True})


@app.route('/mdblists/delete', methods=['POST'])
def delete_mdblist():
    data = request.json
    fn = _safe_filename(data.get('name', ''))
    if not fn:
        return jsonify({'error': 'Invalid filename'}), 400
    config = load_config()
    d = config.get('mdblists', {}).get('directory', 'mdblists')
    p = os.path.join(BASE_DIR, d)
    fp = os.path.join(p, fn)
    if os.path.exists(fp):
        # Derive collection name before deleting
        col_name = os.path.splitext(fn)[0]
        try:
            import yaml as _yaml
            with open(fp, 'r', encoding='utf-8') as fh:
                parsed = _yaml.safe_load(fh.read())
            if isinstance(parsed, dict) and parsed.get('collection_name'):
                col_name = parsed['collection_name']
        except Exception:
            pass
        os.remove(fp)
        # Clean up enabled state
        state = load_state()
        cur = set(state.get('enabled_recipes', []))
        cur.discard(col_name)
        state['enabled_recipes'] = list(cur)
        save_state(state)
        # Clean up override
        try:
            from src.recipe_override import RecipeOverrideManager
            mgr = RecipeOverrideManager(BASE_DIR)
            mgr.delete_override(col_name)
        except Exception:
            pass
        # Clean up list metadata
        try:
            from src.list_metadata import ListMetadataManager
            meta_mgr = ListMetadataManager(BASE_DIR)
            meta_mgr.delete_list_config('mdblists', fn)
        except Exception:
            pass
        return jsonify({'success': True})
    return jsonify({'error': 'File not found'}), 404




# === Recipe Detail / Override / Duplicate ===

@app.route('/api/recipe_detail/<path:recipe_name>')
def recipe_detail(recipe_name):
    """Get full details of a recipe including override config."""
    from src.collection_recipes import RECIPES, CATEGORY_CONFIG
    recipe = None
    for r in RECIPES:
        if r.get('name') == recipe_name:
            recipe = r
            break
    if not recipe:
        return jsonify({'error': 'Recipe not found'}), 404
    
    # Get override
    from src.recipe_override import RecipeOverrideManager
    mgr = RecipeOverrideManager(BASE_DIR)
    override = mgr.get_override(recipe_name)
    
    # Get category name
    cat_id = recipe.get('category_id', 0)
    cat_name = CATEGORY_CONFIG.get(cat_id, {}).get('name', 'Unknown')
    
    # Build detail dict (exclude non-serializable stuff)
    detail = {
        'name': recipe.get('name'),
        'source_type': recipe.get('source_type'),
        'category_id': cat_id,
        'category_name': cat_name,
        'item_limit': recipe.get('item_limit'),
        'tmdb_discover_params': recipe.get('tmdb_discover_params'),
        'tmdb_collection_id': recipe.get('tmdb_collection_id'),
        'trakt_list_params': recipe.get('trakt_list_params'),
        'sort_by': recipe.get('sort_by'),
        'override': override,
    }
    return jsonify(detail)


@app.route('/api/recipe_override', methods=['POST'])
def save_recipe_override():
    """Save override config for a recipe."""
    data = request.json
    recipe_name = data.get('recipe_name')
    override = data.get('override', {})
    if not recipe_name:
        return jsonify({'error': 'No recipe_name'}), 400
    from src.recipe_override import RecipeOverrideManager
    mgr = RecipeOverrideManager(BASE_DIR)
    mgr.set_override(recipe_name, override)
    return jsonify({'success': True})


@app.route('/api/recipe_override_delete', methods=['POST'])
def delete_recipe_override():
    """Delete override for a recipe."""
    data = request.json
    recipe_name = data.get('recipe_name')
    if not recipe_name:
        return jsonify({'error': 'No recipe_name'}), 400
    from src.recipe_override import RecipeOverrideManager
    mgr = RecipeOverrideManager(BASE_DIR)
    mgr.delete_override(recipe_name)
    return jsonify({'success': True})


@app.route('/api/recipe_duplicate', methods=['POST'])
def duplicate_recipe():
    """Create a duplicate of a recipe."""
    data = request.json
    original_name = data.get('original_name')
    new_name = data.get('new_name')
    if not original_name or not new_name:
        return jsonify({'error': 'Missing original_name or new_name'}), 400
    # Validate that the original recipe exists
    from src.collection_recipes import RECIPES
    if not any(r.get('name') == original_name for r in RECIPES):
        return jsonify({'error': f'Original recipe not found: {original_name}'}), 400
    # Validate that new_name doesn't collide with existing recipes or duplicates
    if any(r.get('name') == new_name for r in RECIPES):
        return jsonify({'error': f'A recipe with this name already exists: {new_name}'}), 400
    from src.recipe_override import RecipeOverrideManager
    mgr = RecipeOverrideManager(BASE_DIR)
    existing_dups = mgr.get_duplicates()
    if any(d.get('new_name') == new_name for d in existing_dups):
        return jsonify({'error': f'A duplicate with this name already exists: {new_name}'}), 400
    duplicate = {
        'original_name': original_name,
        'new_name': new_name,
        'library_ids': data.get('library_ids', []),
        'extra_mdblist_urls': data.get('extra_mdblist_urls', []),
        'extra_trakt_urls': data.get('extra_trakt_urls', []),
        'extra_tmdb_ids': data.get('extra_tmdb_ids', []),
        'custom_poster_url': data.get('custom_poster_url'),
        'custom_backdrop_url': data.get('custom_backdrop_url'),
    }
    idx = mgr.add_duplicate(duplicate)
    return jsonify({'success': True, 'index': idx})


@app.route('/api/recipe_duplicates')
def list_duplicates():
    """List all duplicate recipes."""
    from src.recipe_override import RecipeOverrideManager
    mgr = RecipeOverrideManager(BASE_DIR)
    return jsonify(mgr.get_duplicates())


@app.route('/api/recipe_duplicate_delete', methods=['POST'])
def delete_duplicate():
    """Delete a duplicate recipe by index or new_name.
    Also cleans up orphaned enabled state and overrides."""
    data = request.json
    index = data.get('index')
    new_name = data.get('new_name')
    if index is None and not new_name:
        return jsonify({'error': 'No index or new_name'}), 400
    from src.recipe_override import RecipeOverrideManager
    mgr = RecipeOverrideManager(BASE_DIR)
    # Find the duplicate's new_name for cleanup
    duplicates = mgr.get_duplicates()
    dup_new_name = None
    if index is not None:
        try:
            idx = int(index)
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid index'}), 400
        if 0 <= idx < len(duplicates):
            dup_new_name = duplicates[idx].get('new_name')
            mgr.delete_duplicate(idx)
        else:
            return jsonify({'error': 'Invalid index'}), 400
    else:
        # Find by new_name
        for i, dup in enumerate(duplicates):
            if dup.get('new_name') == new_name:
                dup_new_name = dup.get('new_name')
                mgr.delete_duplicate(i)
                break
        if not dup_new_name:
            return jsonify({'error': 'Duplicate not found'}), 404
    # Clean up orphaned enabled state
    if dup_new_name:
        state = load_state()
        cur = set(state.get('enabled_recipes', []))
        if dup_new_name in cur:
            cur.discard(dup_new_name)
            state['enabled_recipes'] = list(cur)
            save_state(state)
        # Clean up any override for the duplicate's new_name
        try:
            mgr.delete_override(dup_new_name)
        except Exception:
            pass
    return jsonify({'success': True})


# === Artwork Management ===

@app.route('/api/collection_image_proxy/<collection_id>')
def collection_image_proxy(collection_id):
    """Proxy collection images through Flask so the browser can access them
    regardless of the internal Emby server URL."""
    config = load_config()
    emby_cfg = config.get('emby', {})
    if not emby_cfg.get('server_url') or not emby_cfg.get('api_key'):
        return jsonify({'error': 'Emby not configured'}), 400
    image_type = _valid_image_type(request.args.get('type', 'Primary'))
    if not image_type:
        return Response(b'', status=400, content_type='image/jpeg')
    try:
        import requests as _requests
        url = f"{emby_cfg['server_url'].rstrip('/')}/Items/{collection_id}/Images/{image_type}?api_key={emby_cfg['api_key']}"
        resp = _requests.get(url, timeout=15)
        if resp.status_code == 200 and resp.content:
            return Response(resp.content, content_type=resp.headers.get('Content-Type', 'image/jpeg'))
        else:
            return Response(b'', status=404, content_type='image/jpeg')
    except Exception as e:
        logger.error(f"Image proxy error: {e}")
        return Response(b'', status=500, content_type='image/jpeg')


@app.route('/artwork')
def artwork():
    """Show all Emby collections with artwork management."""
    config = load_config()
    emby_cfg = config.get('emby', {})
    if not emby_cfg.get('server_url') or not emby_cfg.get('api_key'):
        return render_template('artwork.html', collections=[], error="Emby not configured")
    try:
        from src.emby_client import EmbyClient
        emby = EmbyClient(
            server_url=emby_cfg['server_url'],
            api_key=emby_cfg['api_key'],
            user_id=emby_cfg.get('user_id', ''),
            config=config
        )
        collections = emby.get_all_collections()
        # Build image URLs that proxy through Flask (so the browser can access them
        # regardless of the internal Emby server URL)
        for col in collections:
            col_id = col.get('Id', '')
            col['poster_url'] = f"/api/collection_image_proxy/{col_id}?type=Primary"
            col['backdrop_url'] = f"/api/collection_image_proxy/{col_id}?type=Backdrop"
        return render_template('artwork.html', collections=collections, error=None)
    except Exception as e:
        return render_template('artwork.html', collections=[], error=str(e))


@app.route('/api/collection_image', methods=['POST'])
def get_collection_image():
    """Get current collection image as base64 for preview."""
    data = request.json
    collection_id = data.get('collection_id')
    image_type = _valid_image_type(data.get('image_type', 'Primary'))
    if not collection_id:
        return jsonify({'error': 'No collection_id'}), 400
    if not image_type:
        return jsonify({'error': 'Invalid image type'}), 400
    try:
        config = load_config()
        emby_cfg = config.get('emby', {})
        from src.emby_client import EmbyClient
        emby = EmbyClient(
            server_url=emby_cfg['server_url'],
            api_key=emby_cfg['api_key'],
            user_id=emby_cfg.get('user_id', ''),
            config=config
        )
        result = emby.get_collection_image(collection_id, image_type)
        return jsonify(result or {'has_image': False})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/upload_image', methods=['POST'])
def upload_image():
    """Upload a custom image for a collection."""
    collection_id = request.form.get('collection_id')
    image_type = _valid_image_type(request.form.get('image_type', 'Primary'))
    if not image_type:
        return jsonify({'error': 'Invalid image type'}), 400
    if 'file' not in request.files or not collection_id:
        return jsonify({'error': 'No file or collection_id'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    # Validate file type
    allowed = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in allowed:
        return jsonify({'error': f'Invalid file type. Allowed: {allowed}'}), 400
    content_types = {
        'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
        'gif': 'image/gif', 'webp': 'image/webp'
    }
    content_type = content_types.get(ext, 'image/jpeg')
    image_data = file.read()
    # Limit to 10MB
    if len(image_data) > 10 * 1024 * 1024:
        return jsonify({'error': 'File too large (max 10MB)'}), 400
    try:
        config = load_config()
        emby_cfg = config.get('emby', {})
        from src.emby_client import EmbyClient
        emby = EmbyClient(
            server_url=emby_cfg['server_url'],
            api_key=emby_cfg['api_key'],
            user_id=emby_cfg.get('user_id', ''),
            config=config
        )
        success = emby.upload_collection_image_from_data(collection_id, image_data, image_type, content_type)
        if success:
            return jsonify({'success': True})
        return jsonify({'error': 'Upload failed'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/reset_image', methods=['POST'])
def reset_image():
    """Reset a collection image to default (delete custom image)."""
    data = request.json
    collection_id = data.get('collection_id')
    image_type = _valid_image_type(data.get('image_type', 'Primary'))
    if not collection_id:
        return jsonify({'error': 'No collection_id'}), 400
    if not image_type:
        return jsonify({'error': 'Invalid image type'}), 400
        return jsonify({'error': 'No collection_id'}), 400
    try:
        config = load_config()
        emby_cfg = config.get('emby', {})
        from src.emby_client import EmbyClient
        emby = EmbyClient(
            server_url=emby_cfg['server_url'],
            api_key=emby_cfg['api_key'],
            user_id=emby_cfg.get('user_id', ''),
            config=config
        )
        success = emby.delete_collection_image(collection_id, image_type)
        if success:
            return jsonify({'success': True})
        return jsonify({'error': 'Reset failed'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# === Per-Collection Library Selection ===

@app.route('/api/list_config', methods=['GET', 'POST'])
def list_config():
    """Get or set per-collection config (library_ids, custom name, etc)."""
    if request.method == 'GET':
        list_type = request.args.get('type', 'traktlists')
        from src.list_metadata import ListMetadataManager
        mgr = ListMetadataManager(BASE_DIR)
        configs = mgr.get_all_configs(list_type)
        return jsonify(configs)
    else:
        data = request.json
        list_type = data.get('type')
        filename = data.get('filename')
        config = data.get('config', {})
        if not list_type or not filename:
            return jsonify({'error': 'Missing type or filename'}), 400
        from src.list_metadata import ListMetadataManager
        mgr = ListMetadataManager(BASE_DIR)
        mgr.set_list_config(list_type, filename, config)
        return jsonify({'success': True})


@app.route('/api/list_libraries')
def list_libraries():
    """Get available Emby libraries (for per-collection selection)."""
    libs = get_emby_libraries()
    return jsonify(libs)



# === Logs Viewer ===

@app.route('/logs')
def logs_page():
    return render_template('logs.html')


@app.route('/api/logs')
def api_logs():
    n = request.args.get('lines', '100', type=int)
    if _log_provider:
        return jsonify({'lines': _log_provider(n)})
    return jsonify({'lines': ''})


# === Sync History ===

@app.route('/api/sync_history')
def sync_history():
    from src.sync_history import SyncHistory
    h = SyncHistory(BASE_DIR)
    return jsonify(h.get_history(20))


@app.route('/api/sync_history_clear', methods=['POST'])
def sync_history_clear():
    from src.sync_history import SyncHistory
    h = SyncHistory(BASE_DIR)
    h.clear()
    return jsonify({'success': True})


# === Single Recipe Sync ===

@app.route('/api/sync_single', methods=['POST'])
def sync_single():
    data = request.json
    recipe_name = data.get('recipe_name')
    if not recipe_name:
        return jsonify({'error': 'No recipe_name'}), 400
    with sync_lock:
        if sync_state['running']:
            return jsonify({'error': 'A sync is already running'}), 409
        sync_state['running'] = True
    sync_cancel.clear()

    def run_single_recipe():
        sync_state['last_error'] = None
        sync_state['last_status'] = 'running'
        sync_state['last_run'] = datetime.now().isoformat()
        sync_state['progress'] = None
        # Set cancel event and progress callback on app_logic
        try:
            import src.app_logic as app_logic_module
            app_logic_module._cancel_event = sync_cancel
            app_logic_module._progress_callback = _update_progress
        except Exception:
            pass
        try:
            if _sync_function:
                _sync_function(single_recipe=recipe_name)
            else:
                run_sync_background()
            if sync_cancel.is_set():
                sync_state['last_status'] = 'cancelled'
            else:
                sync_state['last_status'] = 'success'
        except Exception as e:
            sync_state['last_status'] = 'error'
            sync_state['last_error'] = str(e)
        finally:
            try:
                import src.app_logic as app_logic_module
                app_logic_module._cancel_event = None
                app_logic_module._progress_callback = None
            except Exception:
                pass
            with sync_lock:
                sync_state['running'] = False
                sync_state['progress'] = None

    thread = threading.Thread(target=run_single_recipe, daemon=True)
    thread.start()
    return jsonify({'success': True, 'message': f'Syncing {recipe_name}'})


# === Next Sync Time ===

@app.route('/api/next_sync')
def next_sync():
    if _next_sync_provider:
        t = _next_sync_provider()
        if t:
            return jsonify({'next_sync': t.isoformat(), 'next_sync_display': t.strftime('%Y-%m-%d %H:%M:%S')})
    return jsonify({'next_sync': None})


# === Export/Import Config ===

@app.route('/api/export_config')
def export_config():
    """Export all config as a single JSON file."""
    import tempfile
    config = load_config()
    state = load_state()
    # Load list metadata
    list_meta = {}
    meta_path = os.path.join(BASE_DIR, 'config', 'list_metadata.json')
    try:
        with open(meta_path, 'r') as f:
            list_meta = json.load(f)
    except Exception:
        pass
    # Load recipe overrides
    recipe_ov = {}
    ov_path = os.path.join(BASE_DIR, 'config', 'recipe_overrides.json')
    try:
        with open(ov_path, 'r') as f:
            recipe_ov = json.load(f)
    except Exception:
        pass
    export = {
        'config': config,
        'state': state,
        'list_metadata': list_meta,
        'recipe_overrides': recipe_ov,
        'exported_at': datetime.now().isoformat(),
        'version': 1,
    }
    # Use a temp file that we clean up after sending
    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
    json.dump(export, tmp, indent=2)
    tmp.close()
    try:
        return send_file(tmp.name, as_attachment=True, download_name='ecm_config_backup.json')
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


@app.route('/api/import_config', methods=['POST'])
def import_config():
    """Import config from a JSON file."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    try:
        data = json.load(file)
        if 'config' in data:
            save_config(data['config'])
        if 'state' in data:
            save_state(data['state'])
        if 'list_metadata' in data:
            meta_path = os.path.join(BASE_DIR, 'config', 'list_metadata.json')
            with open(meta_path, 'w', encoding='utf-8') as f:
                json.dump(data['list_metadata'], f, indent=2)
        if 'recipe_overrides' in data:
            ov_path = os.path.join(BASE_DIR, 'config', 'recipe_overrides.json')
            with open(ov_path, 'w', encoding='utf-8') as f:
                json.dump(data['recipe_overrides'], f, indent=2)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# === Delete Emby Collection ===

@app.route('/api/delete_collection', methods=['POST'])
def delete_collection():
    """Delete a collection from Emby."""
    data = request.json
    collection_id = data.get('collection_id')
    if not collection_id:
        return jsonify({'error': 'No collection_id'}), 400
    try:
        config = load_config()
        emby_cfg = config.get('emby', {})
        from src.emby_client import EmbyClient
        emby = EmbyClient(
            server_url=emby_cfg['server_url'],
            api_key=emby_cfg['api_key'],
            user_id=emby_cfg.get('user_id', ''),
            config=config
        )
        url = f"{emby.server_url}/Items/{collection_id}?api_key={emby.api_key}"
        resp = emby.session.delete(url, timeout=15)
        if resp.status_code in [200, 204]:
            return jsonify({'success': True})
        return jsonify({'error': f'Delete failed: {resp.status_code}'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# === Collection Preview (TMDb IDs before sync) ===

@app.route('/api/preview_collection/<path:recipe_name>')
def preview_collection(recipe_name):
    """Preview what TMDb IDs a recipe would produce without syncing."""
    from src.collection_recipes import RECIPES
    recipe = None
    for r in RECIPES:
        if r.get('name') == recipe_name:
            recipe = r
            break
    if not recipe:
        return jsonify({'error': 'Recipe not found'}), 404

    config = load_config()
    source_type = recipe.get('source_type')
    item_limit = recipe.get('item_limit', 50)
    tmdb_ids = []
    movie_titles = []

    try:
        from src.tmdb_client import TmdbClient
        tmdb = TmdbClient(api_key=config.get('tmdb', {}).get('api_key', ''))

        if source_type in ('tmdb_discover', 'tmdb_discover_individual_movies'):
            params = recipe.get('tmdb_discover_params', {})
            import math as _math
            page_limit = _math.ceil(item_limit / 20) if item_limit else 1
            movies = tmdb.discover_movies(params, page_limit)
            if item_limit and len(movies) > item_limit:
                movies = movies[:item_limit]
            tmdb_ids = [m['id'] for m in movies]
            movie_titles = [{'id': m['id'], 'title': m.get('title', ''), 'year': m.get('release_date', '')[:4]} for m in movies]
        elif source_type in ('tmdb_collection', 'tmdb_series_collection'):
            col_id = recipe.get('tmdb_collection_id')
            if col_id:
                movies = tmdb.get_collection_movies(col_id, item_limit)
                tmdb_ids = [m['id'] for m in movies]
                movie_titles = [{'id': m['id'], 'title': m.get('title', ''), 'year': m.get('release_date', '')[:4]} for m in movies]
        else:
            return jsonify({'error': f'Preview not supported for source type: {source_type}. Use sync to test Trakt-based recipes.'})

        return jsonify({
            'recipe_name': recipe_name,
            'source_type': source_type,
            'count': len(tmdb_ids),
            'movies': movie_titles[:50],  # Limit preview to 50
            'total': len(tmdb_ids),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# === Notifications Settings ===

@app.route('/api/notifications', methods=['GET', 'POST'])
def notifications():
    if request.method == 'POST':
        data = request.json
        config = load_config()
        config.setdefault('notifications', {})['enabled'] = data.get('enabled', False)
        config['notifications']['webhook_url'] = data.get('webhook_url', '')
        config['notifications']['notify_on_success'] = data.get('notify_on_success', True)
        config['notifications']['notify_on_error'] = data.get('notify_on_error', True)
        save_config(config)
        return jsonify({'success': True})
    else:
        config = load_config()
        return jsonify(config.get('notifications', {'enabled': False, 'webhook_url': '', 'notify_on_success': True, 'notify_on_error': True}))


def start_webui(host='0.0.0.0', port=8282):
    logging.basicConfig(level=logging.INFO)
    print(f"Starting Web UI on http://{host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == '__main__':
    port = int(os.environ.get('WEBUI_PORT', '8282'))
    start_webui(port=port)
