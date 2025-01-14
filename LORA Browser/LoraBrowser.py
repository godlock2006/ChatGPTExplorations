# lora_browser.py
import os
import json
import mimetypes
import threading
import webbrowser
from pathlib import Path

from flask import (
    Flask, 
    request,
    send_file, 
    jsonify,
    abort,
    render_template_string
)

#######################################################
# Configuration
#######################################################
LORA_DIRECTORY = r"E:\PATH\TO\LORA\FOLDER"  # Adjust as needed
FAVORITES_JSON = "favorites.json"

# Convert to absolute path:
LORA_DIRECTORY = os.path.abspath(LORA_DIRECTORY)

app = Flask(__name__)

#######################################################
# Favorites
#######################################################
def load_favorites() -> set:
    if not os.path.isfile(FAVORITES_JSON):
        return set()
    try:
        with open(FAVORITES_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data)
    except:
        return set()

def save_favorites(favs: set):
    with open(FAVORITES_JSON, "w", encoding="utf-8") as f:
        json.dump(list(favs), f, ensure_ascii=False, indent=2)

favorites_set = load_favorites()

#######################################################
# .civitai.info Parser
#######################################################
def parse_civitai_info(info_path: str):
    """Return (trained_words, prompt) from a .civitai.info JSON."""
    if not os.path.isfile(info_path):
        return [], None

    try:
        with open(info_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except:
        return [], None

    trained = data.get("trainedWords", [])
    if not isinstance(trained, list):
        trained = []

    prompt_val = None
    images = data.get("images", [])
    if isinstance(images, list):
        for img in images:
            if isinstance(img, dict):
                meta = img.get("meta", {})
                if isinstance(meta, dict) and "prompt" in meta:
                    prompt_val = meta["prompt"]
                    break

    return trained, prompt_val

#######################################################
# Preview Image Check
#######################################################
def get_preview_image_path(safetensors_path):
    """
    Check if either <base>.preview.png or <base>.png exists.
    If neither exists, return None.
    """
    base = os.path.splitext(safetensors_path)[0]
    preview1 = base + ".preview.png"
    preview2 = base + ".png"

    if os.path.isfile(preview1):
        return preview1
    elif os.path.isfile(preview2):
        return preview2
    return None

#######################################################
# Building a Folder Tree
#######################################################
def build_tree(folder_path):
    """Return a nested dict describing the folder structure."""
    try:
        entries = list(os.scandir(folder_path))
    except FileNotFoundError:
        return None

    subfolders = []
    safetensors = []
    for e in entries:
        if e.is_dir():
            subfolders.append(e.path)
        elif e.is_file() and e.name.lower().endswith(".safetensors"):
            safetensors.append(e.path)

    subfolders.sort(key=str.lower)
    safetensors.sort(key=str.lower)

    node = {
        "name": os.path.basename(folder_path),
        "path": folder_path,
        "subfolders": [],
        "files": safetensors
    }
    for sf in subfolders:
        subtree = build_tree(sf)
        if subtree:
            node["subfolders"].append(subtree)
    return node


#######################################################
# Flask Routes
#######################################################

@app.route("/")
def index():
    # We use a single inlined HTML template for simplicity
    html = r"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Lora Browser</title>
  <style>
    body {
      margin: 0; 
      padding: 0; 
      overflow: hidden;
      font-family: sans-serif;
    }
    #container {
      display: flex;
      width: 100%;
      height: 100vh;
    }
    #sidebar {
      width: 250px;
      background: #f0f0f0;
      border-right: 1px solid #ccc;
      overflow-y: auto;
      padding: 8px;
    }
    #main {
      flex: 1;
      display: flex;
      flex-direction: column;
      overflow-y: hidden;
    }
    #searchContainer {
      padding: 8px;
      background: #fafafa;
      border-bottom: 1px solid #ccc;
    }
    #searchContainer input {
      width: 300px;
      padding: 4px;
      font-size: 14px;
    }
    #contentArea {
      flex: 1;
      overflow-y: auto;
      padding: 8px;
    }
    /* A grid of 6 columns, auto-scaled images: */
    .fileGrid {
      display: grid;
      grid-template-columns: repeat(6, minmax(0,1fr));
      gap: 16px;
    }
    .fileCard {
      border: 1px solid #ddd;
      padding: 4px;
      text-align: center;
      background: #fff;
      display: flex;
      flex-direction: column;
      align-items: center;
    }
    .previewImg {
      width: 100%;
      height: auto;
      object-fit: contain; /* Keep aspect ratio */
    }
    .placeholder {
      position: relative;
      width: 100%;
      padding-bottom: 100%; /* square */
      background: #ccc;
    }
    .fileName {
      font-weight: bold;
      margin: 6px 0;
      font-size: 0.9rem;
      text-align: center;
    }
    .buttonsRow {
      display: flex;
      gap: 4px;
      justify-content: space-between;
      margin: 6px 0 0 0;
      width: 100%;
    }
    .copyBtn {
      border: none;
      cursor: pointer;
      flex: 1;
      padding: 4px;
      color: #fff;
      font-size: 0.8rem;
      text-transform: uppercase;
    }
    .redBtn    { background: #ff4b4b; }
    .blueBtn   { background: #0066ff; }
    .greenBtn  { background: #49a349; }
    .yellowBtn { background: #ffcc00; color: #000; }
    .favoriteMsg {
      color: green;
      font-size: 0.75rem;
      margin-top: 4px;
    }
    #sidebar button {
      display: block;
      width: 100%;
      margin-bottom: 4px;
      font-size: 14px;
      padding: 4px;
      cursor: pointer;
    }
    .treeItem {
      margin: 2px 0;
      margin-left: 12px;
    }
    .collapseToggle {
      cursor: pointer;
      margin-right: 4px;
      color: #888;
    }
    .collapseToggle:hover {
      color: #000;
    }
    .subfolderList {
      margin-left: 16px;
      border-left: 1px dashed #ccc;
      padding-left: 8px;
    }
  </style>
</head>
<body>
  <div id="container">
    <div id="sidebar">
      <button onclick="loadRoot()">Top Folder</button>
      <button onclick="showFavorites()">SHOW FAVORITES</button>
      <hr/>
      <div id="folderTree"></div>
    </div>
    <div id="main">
      <div id="searchContainer">
        <input type="text" id="searchInput" placeholder="Type to search..." oninput="onSearchChange()"/>
      </div>
      <div id="contentArea"></div>
    </div>
  </div>

<script>
  const LORA_DIRECTORY = {{ lora_dir | tojson }};
  let currentPath = LORA_DIRECTORY; 
  let favorites = {{ favorites | tojson }};
  let folderTreeData = null;

  // On page load, fetch the entire tree & render
  window.onload = async function() {
    const tree = await fetch('/api/tree').then(res => res.json());
    folderTreeData = tree;
    drawFolderTree(document.getElementById("folderTree"), folderTreeData, true);
    loadRoot();
  };

  // Build clickable folder tree
  function drawFolderTree(container, node, isRoot=false) {
    if (!node) return;

    const itemDiv = document.createElement('div');
    itemDiv.className = 'treeItem';

    if (node.subfolders && node.subfolders.length > 0) {
      const toggle = document.createElement('span');
      toggle.textContent = '[–]';
      toggle.className = 'collapseToggle';
      let collapsed = false;
      toggle.onclick = () => {
        collapsed = !collapsed;
        toggle.textContent = collapsed ? '[+]' : '[–]';
        subfolderDiv.style.display = collapsed ? 'none' : 'block';
      };
      itemDiv.appendChild(toggle);
    }

    const folderSpan = document.createElement('span');
    folderSpan.textContent = node.name;
    folderSpan.style.cursor = "pointer";
    folderSpan.onclick = () => {
      currentPath = node.path;
      showFolder(node.path);
    };
    itemDiv.appendChild(folderSpan);

    container.appendChild(itemDiv);

    if (node.subfolders && node.subfolders.length > 0) {
      const subfolderDiv = document.createElement('div');
      subfolderDiv.className = 'subfolderList';
      node.subfolders.forEach(sf => {
        drawFolderTree(subfolderDiv, sf, false);
      });
      container.appendChild(subfolderDiv);
    }
  }

  function loadRoot() {
    currentPath = LORA_DIRECTORY;
    showFolder(LORA_DIRECTORY);
  }

  async function showFolder(folderPath) {
    currentPath = folderPath;
    document.getElementById("searchInput").value = "";
    const files = await fetch(`/api/files?folder=${encodeURIComponent(folderPath)}`).then(r => r.json());
    drawFilesGrid(files);
  }

  async function showFavorites() {
    currentPath = "FAVORITES";
    document.getElementById("searchInput").value = "";
    const favs = await fetch(`/api/favorites/files`).then(r => r.json());
    drawFilesGrid(favs);
  }

  function onSearchChange() {
    const q = document.getElementById("searchInput").value.trim().toLowerCase();
    if (!q) {
      if (currentPath === "FAVORITES") {
        showFavorites();
      } else {
        showFolder(currentPath);
      }
      return;
    }
    fetch(`/api/search?query=${encodeURIComponent(q)}&folder=${encodeURIComponent(currentPath)}`)
      .then(res => res.json())
      .then(files => {
        drawFilesGrid(files);
      });
  }

  function drawFilesGrid(files) {
    const contentArea = document.getElementById("contentArea");
    contentArea.innerHTML = "";

    const grid = document.createElement("div");
    grid.className = "fileGrid";

    files.forEach(fobj => {
      // { safetensors_path, filename_no_ext, preview, trainedWords, prompt }
      const card = document.createElement("div");
      card.className = "fileCard";

      if (fobj.preview) {
        const img = document.createElement("img");
        img.className = "previewImg";
        img.src = `/api/preview?path=${encodeURIComponent(fobj.preview)}`;
        card.appendChild(img);
      } else {
        const ph = document.createElement("div");
        ph.className = "placeholder";
        card.appendChild(ph);
      }

      const fnameDiv = document.createElement("div");
      fnameDiv.className = "fileName";
      fnameDiv.textContent = fobj.filename_no_ext;
      card.appendChild(fnameDiv);

      const btnRow = document.createElement("div");
      btnRow.className = "buttonsRow";

      // LORA
      const bLora = document.createElement("button");
      bLora.className = "copyBtn redBtn";
      bLora.textContent = "LORA";
      bLora.onclick = () => copyToClipboard(`<lora:${fobj.filename_no_ext}:1>`);
      btnRow.appendChild(bLora);

      // Trigger
      const bTrigger = document.createElement("button");
      bTrigger.className = "copyBtn blueBtn";
      bTrigger.textContent = "Trigger";
      bTrigger.onclick = () => copyToClipboard(fobj.trainedWords.join(" "));
      btnRow.appendChild(bTrigger);

      // Prompt
      const bPrompt = document.createElement("button");
      bPrompt.className = "copyBtn greenBtn";
      bPrompt.textContent = "Prompt";
      bPrompt.onclick = () => copyToClipboard(fobj.prompt || "");
      btnRow.appendChild(bPrompt);

      // Favorite
      const bFav = document.createElement("button");
      bFav.className = "copyBtn yellowBtn";
      bFav.textContent = "FAVORITE";
      bFav.onclick = () => toggleFavorite(fobj.filename_no_ext);
      btnRow.appendChild(bFav);

      card.appendChild(btnRow);

      if (favorites.includes(fobj.filename_no_ext)) {
        const favMsg = document.createElement("div");
        favMsg.className = "favoriteMsg";
        favMsg.textContent = "Currently in Favorites";
        card.appendChild(favMsg);
      }

      grid.appendChild(card);
    });

    contentArea.appendChild(grid);
  }

  function copyToClipboard(text) {
    navigator.clipboard.writeText(text).then(() => {
      alert("Copied: " + text);
    }).catch(err => {
      alert("Failed to copy: " + err);
    });
  }

  function toggleFavorite(baseName) {
    fetch("/api/favorites/toggle", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ name_no_ext: baseName })
    })
    .then(r => r.json())
    .then(data => {
      favorites = data.favorites;
      if (currentPath === "FAVORITES") {
        showFavorites();
      } else {
        showFolder(currentPath);
      }
    });
  }
</script>
</body>
</html>
    """
    return render_template_string(
        html,
        lora_dir=LORA_DIRECTORY,
        favorites=list(favorites_set)
    )

@app.route("/api/tree")
def api_tree():
    tree = build_tree(LORA_DIRECTORY)
    return jsonify(tree)

@app.route("/api/files")
def api_files():
    folder = request.args.get("folder", LORA_DIRECTORY)
    folder = os.path.abspath(folder)
    if not folder.startswith(LORA_DIRECTORY):
        abort(400, "Invalid folder path")

    safes = []
    for root, _, files in os.walk(folder):
        for f in files:
            if f.lower().endswith(".safetensors"):
                safes.append(os.path.join(root, f))

    out = []
    safes.sort(key=str.lower)
    for sf in safes:
        base_no_ext = os.path.splitext(os.path.basename(sf))[0]
        preview = get_preview_image_path(sf)
        trained, prompt = parse_civitai_info(sf.replace(".safetensors", ".civitai.info"))
        out.append({
            "safetensors_path": sf,
            "filename_no_ext": base_no_ext,
            "preview": preview,
            "trainedWords": trained,
            "prompt": prompt
        })
    return jsonify(out)

@app.route("/api/preview")
def api_preview():
    p = request.args.get("path", "")
    p = os.path.abspath(p)
    if not p.startswith(LORA_DIRECTORY):
        abort(400, "Invalid path")

    if not os.path.isfile(p):
        abort(404, "Not found")

    mime, _ = mimetypes.guess_type(p)
    if not mime:
        mime = "image/png"
    return send_file(p, mimetype=mime)

@app.route("/api/favorites/files")
def api_favorites_files():
    matched = []
    for root, _, files in os.walk(LORA_DIRECTORY):
        for f in files:
            if f.lower().endswith(".safetensors"):
                base_no_ext = os.path.splitext(f)[0]
                if base_no_ext in favorites_set:
                    fullpath = os.path.join(root, f)
                    preview = get_preview_image_path(fullpath)
                    trained, prompt = parse_civitai_info(fullpath.replace(".safetensors", ".civitai.info"))
                    matched.append({
                        "safetensors_path": fullpath,
                        "filename_no_ext": base_no_ext,
                        "preview": preview,
                        "trainedWords": trained,
                        "prompt": prompt
                    })
    matched.sort(key=lambda x: x["filename_no_ext"].lower())
    return jsonify(matched)

@app.route("/api/favorites/toggle", methods=["POST"])
def api_favorites_toggle():
    global favorites_set
    data = request.get_json(force=True)
    name_no_ext = data.get("name_no_ext", "")
    if name_no_ext in favorites_set:
        favorites_set.remove(name_no_ext)
    else:
        favorites_set.add(name_no_ext)
    save_favorites(favorites_set)
    return jsonify({"favorites": list(favorites_set)})

@app.route("/api/search")
def api_search():
    q = request.args.get("query", "").strip().lower()
    folder = request.args.get("folder", LORA_DIRECTORY)
    folder = os.path.abspath(folder)

    # Searching favorites or a real folder?
    if folder == "FAVORITES":
        matched = []
        for root, _, files in os.walk(LORA_DIRECTORY):
            for f in files:
                if f.lower().endswith(".safetensors"):
                    bn = os.path.splitext(f)[0]
                    if bn in favorites_set and q in bn.lower():
                        fp = os.path.join(root, f)
                        preview = get_preview_image_path(fp)
                        trained, prompt = parse_civitai_info(fp.replace(".safetensors", ".civitai.info"))
                        matched.append({
                            "safetensors_path": fp,
                            "filename_no_ext": bn,
                            "preview": preview,
                            "trainedWords": trained,
                            "prompt": prompt
                        })
        matched.sort(key=lambda x: x["filename_no_ext"].lower())
        return jsonify(matched)
    else:
        if not folder.startswith(LORA_DIRECTORY):
            abort(400, "Invalid folder path")

        all_safes = []
        for root, _, files in os.walk(folder):
            for f in files:
                if f.lower().endswith(".safetensors"):
                    all_safes.append(os.path.join(root, f))

        matched = []
        for sf in all_safes:
            bn = os.path.splitext(os.path.basename(sf))[0]
            if q in bn.lower():
                preview = get_preview_image_path(sf)
                trained, prompt = parse_civitai_info(sf.replace(".safetensors", ".civitai.info"))
                matched.append({
                    "safetensors_path": sf,
                    "filename_no_ext": bn,
                    "preview": preview,
                    "trainedWords": trained,
                    "prompt": prompt
                })
        matched.sort(key=lambda x: x["filename_no_ext"].lower())
        return jsonify(matched)

if __name__ == "__main__":
    # Auto-open the default browser for convenience
    def open_browser():
        webbrowser.open("http://127.0.0.1:5000")

    import webbrowser
    import threading
    threading.Timer(1.0, open_browser).start()

    # Start the Flask app
    app.run(port=5000, debug=False)
