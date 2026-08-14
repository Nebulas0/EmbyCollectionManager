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
from flask import Flask, render_template, request, jsonify, redirect, url_for

# Add parent directory to path so we can import src modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config_loader import ConfigLoader
from src.collection_recipes import RECIPES, CATEGORY_CONFIG

logger = logging.getLogger("WebUI")

app = Flask(__name__)

# Paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, 'config', 'config.yaml')
STATE_PATH = os.path.join(BASE_DIR, 'config', 'webui_state.json')

# Sync state tracking
sync_state = {
    'running': False,
    'last_run': None,
    'last_status': None,
    'last_error': None,
    'thread': None
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
    global sync_state
    sync_state['running'] = True
    sync_state['last_error'] = None
    sync_state['last_status'] = 'running'
    sync_state['last_run'] = datetime.now().isoformat()
    try:
        from src.app_logic import main as app_main
        import src.app_logic as app_logic_module
        state = load_state()
        enabled = state.get('enabled_recipes', None)
        original_recipes = app_logic_module.RECIPES
        if enabled is not None:
            app_logic_module.RECIPES = [r for r in original_recipes if r.get('name') in enabled]
        old_argv = sys.argv
        sys.argv = ['app_logic', '--config', CONFIG_PATH, '--targets', 'auto']
        try:
            app_main()
        finally:
            sys.argv = old_argv
            app_logic_module.RECIPES = original_recipes
        sync_state['last_status'] = 'success'
    except Exception as e:
        sync_state['last_status'] = 'error'
        sync_state['last_error'] = str(e)
        logger.error(f"Sync error: {e}")
    finally:
        sync_state['running'] = False


# === Routes ===

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


@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
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
        max_trakt = request.form.get('traktlists_max_items', '0')
        config['traktlists']['max_items_per_collection'] = int(max_trakt) if max_trakt.isdigit() else 0
        config.setdefault('mdblists', {})['enabled'] = request.form.get('mdblists_enabled') == 'on'
        config['mdblists']['directory'] = request.form.get('mdblists_directory', 'mdblists')
        max_mdb = request.form.get('mdblists_max_items', '0')
        config['mdblists']['max_items_per_collection'] = int(max_mdb) if max_mdb.isdigit() else 0
        config.setdefault('poster_settings', {})['enable_custom_posters'] = request.form.get('poster_enable') == 'on'
        config['poster_settings']['template_name'] = request.form.get('poster_template', 'default.png')
        tp = request.form.get('poster_text_position', '0.5')
        config['poster_settings']['text_position'] = float(tp) if tp else 0.5
        config['poster_settings']['text_color'] = [
            int(request.form.get('text_color_r', 255)),
            int(request.form.get('text_color_g', 255)),
            int(request.form.get('text_color_b', 255))
        ]
        config['poster_settings']['bg_color'] = [
            int(request.form.get('bg_color_r', 0)),
            int(request.form.get('bg_color_g', 0)),
            int(request.form.get('bg_color_b', 0)),
            int(request.form.get('bg_color_a', 128))
        ]
        state = load_state()
        si = request.form.get('sync_interval', '24')
        state['sync_interval_hours'] = int(si) if si.isdigit() else 24
        save_state(state)
        if save_config(config):
            return render_template('settings.html', config=config, state=state, saved=True)
        return render_template('settings.html', config=config, state=state, error="Failed to save config")
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


@app.route('/api/sync', methods=['POST'])
def trigger_sync():
    if sync_state['running']:
        return jsonify({'error': 'Sync already running'}), 409
    thread = threading.Thread(target=run_sync_background, daemon=True)
    sync_state['thread'] = thread
    thread.start()
    return jsonify({'success': True, 'message': 'Sync started'})


@app.route('/api/sync_status')
def sync_status():
    return jsonify({
        'running': sync_state['running'],
        'last_run': sync_state['last_run'],
        'last_status': sync_state['last_status'],
        'last_error': sync_state['last_error']
    })


@app.route('/api/test_emby', methods=['POST'])
def test_emby():
    data = request.json
    try:
        from src.emby_client import EmbyClient
        emby = EmbyClient(server_url=data.get('server_url', ''), api_key=data.get('api_key', ''), user_id=data.get('user_id', ''), config={})
        libs = emby.get_libraries()
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
        return jsonify({'success': True, 'movie_count': len(movies), 'sample': movies[0]['title'] if movies else None})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/test_trakt', methods=['POST'])
def test_trakt():
    data = request.json
    try:
        from src.trakt_client import TraktClient
        trakt = TraktClient(client_id=data.get('client_id', ''))
        trending = trakt.get_trending_lists(1)
        return jsonify({'success': True, 'list_count': len(trending) if trending else 0})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/test_mdblist', methods=['POST'])
def test_mdblist():
    data = request.json
    try:
        from src.mdblist_client import MDBListClient
        client = MDBListClient(api_key=data.get('api_key', ''))
        result = client._make_request('/user/')
        return jsonify({'success': True, 'data': result is not None})
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
    files = []
    if os.path.isdir(p):
        for f in sorted(os.listdir(p)):
            if f.endswith(('.txt', '.yaml', '.yml')):
                with open(os.path.join(p, f), 'r', encoding='utf-8') as fh:
                    c = fh.read()
                files.append({'name': f, 'content': c, 'lines': len(c.splitlines())})
    return render_template('traktlists.html', files=files, directory=d)


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
    with open(os.path.join(p, fn), 'w', encoding='utf-8') as f:
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
        os.remove(fp)
        return jsonify({'success': True})
    return jsonify({'error': 'File not found'}), 404


@app.route('/mdblists')
def mdblists():
    config = load_config()
    d = config.get('mdblists', {}).get('directory', 'mdblists')
    p = os.path.join(BASE_DIR, d)
    files = []
    if os.path.isdir(p):
        for f in sorted(os.listdir(p)):
            if f.endswith(('.txt', '.yaml', '.yml')):
                with open(os.path.join(p, f), 'r', encoding='utf-8') as fh:
                    c = fh.read()
                files.append({'name': f, 'content': c, 'lines': len(c.splitlines())})
    return render_template('mdblists.html', files=files, directory=d)


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
    with open(os.path.join(p, fn), 'w', encoding='utf-8') as f:
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
        os.remove(fp)
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
    from src.recipe_override import RecipeOverrideManager
    mgr = RecipeOverrideManager(BASE_DIR)
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
    """Delete a duplicate recipe by index."""
    data = request.json
    index = data.get('index')
    if index is None:
        return jsonify({'error': 'No index'}), 400
    from src.recipe_override import RecipeOverrideManager
    mgr = RecipeOverrideManager(BASE_DIR)
    mgr.delete_duplicate(int(index))
    return jsonify({'success': True})


# === Artwork Management ===

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
        # Build image URLs for each collection
        base_url = emby_cfg['server_url'].rstrip('/')
        api_key = emby_cfg['api_key']
        for col in collections:
            col_id = col.get('Id', '')
            col['poster_url'] = f"{base_url}/Items/{col_id}/Images/Primary?api_key={api_key}"
            col['backdrop_url'] = f"{base_url}/Items/{col_id}/Images/Backdrop?api_key={api_key}"
        return render_template('artwork.html', collections=collections, error=None)
    except Exception as e:
        return render_template('artwork.html', collections=[], error=str(e))


@app.route('/api/collection_image', methods=['POST'])
def get_collection_image():
    """Get current collection image as base64 for preview."""
    data = request.json
    collection_id = data.get('collection_id')
    image_type = data.get('image_type', 'Primary')
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
        result = emby.get_collection_image(collection_id, image_type)
        return jsonify(result or {'has_image': False})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/upload_image', methods=['POST'])
def upload_image():
    """Upload a custom image for a collection."""
    collection_id = request.form.get('collection_id')
    image_type = request.form.get('image_type', 'Primary')
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
    image_type = data.get('image_type', 'Primary')
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


def start_webui(host='0.0.0.0', port=8282):
    logging.basicConfig(level=logging.INFO)
    print(f"Starting Web UI on http://{host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == '__main__':
    port = int(os.environ.get('WEBUI_PORT', '8282'))
    start_webui(port=port)
