#!/usr/bin/env python3

import os
import re
import json
import streamlit as st
from pathlib import Path
from PIL import Image, PngImagePlugin

#####################################
# CONFIGURATION
#####################################
ROOT_IMAGE_DIR = "\PATH\TO\IMAGE\FOLDER"   # <-- Change this to your folder
CACHE_FILE = "metadata_cache.json"       # JSON file to store image metadata

#####################################
# PROMPT PARSING & CLEANING
#####################################
def parse_positive_prompt(metadata_text: str) -> str:
    """
    Extract everything before 'Negative prompt:' as the positive prompt.
    If not found, use the entire text as fallback.
    """
    match = re.search(r"(.*)Negative prompt:", metadata_text, re.IGNORECASE | re.DOTALL)
    if match:
        positive_part = match.group(1).strip()
        return positive_part
    else:
        return metadata_text.strip()

def clean_prompt_text(prompt: str) -> str:
    """
    Remove all parentheses and also remove patterns like 'character:1.6'.
    The user said:
      1) Parentheses should be removed.
      2) Alphanumeric text followed by a colon, then a number between 1 and 2,
         always wrapped in parentheses, should have the colon and numbers removed
         (plus parentheses removed).

    We'll interpret carefully:
      - First remove parentheses entirely.
      - Then remove ':[some decimal up to 2]' if it exists, e.g. ':1.3' or ':2'.
      - If a tag is 'character:1.6', after removing parentheses the text becomes 'character:1.6'.
        Then we remove the colon and the numeric portion so it becomes 'character'.

    We'll do this in a stepwise manner.
    """
    # 1) Remove parentheses
    no_parens = re.sub(r"[()]", "", prompt)

    # 2) Remove patterns like ':1.6' or ':1.2' or ':2' after removing parentheses
    #    We'll match a colon followed by one or more digits, optionally a decimal, and digits.
    #    We'll remove the entire part. For example: 'character:1.6' -> 'character'
    cleaned = re.sub(r':[0-9]+(\.[0-9]+)?', '', no_parens)

    return cleaned

def tokenize_prompt(prompt: str):
    """
    Split on commas, strip whitespace, lowercase everything, remove duplicates.
    """
    raw_tags = prompt.split(",")
    tags = [t.strip().lower() for t in raw_tags if t.strip()]
    # Remove duplicates by converting to set, then back to list
    return list(set(tags))


#####################################
# JSON CACHE LOGIC
#####################################
def load_cache():
    """
    Load cached metadata from JSON if it exists.
    Returns a dict like:
      {
        "/path/to/image.png": {
          "tags": [...],
          "mtime": 1234567.890
        },
        ...
      }
    """
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save_cache(cache_data):
    """
    Save the cache dict to JSON.
    """
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache_data, f, ensure_ascii=False, indent=2)

#####################################
# MAIN IMAGE/TAG LOADING
#####################################
@st.cache_data
def load_images_and_tags(root_dir):
    """
    On startup, the app scans all .png files under root_dir.
    For each file:
      - If it's in the JSON cache with unchanged mtime, skip re-parse.
      - Otherwise, read the PNG metadata, parse out the positive prompt,
        clean it, tokenize, then store in the cache.
    Finally returns (images_data, all_tags):
      images_data: list of { "path": <str>, "tags": <list[str]> }
      all_tags: sorted list of all unique tags across all images
    """
    cache_dict = load_cache()
    images_data = []
    all_tags = set()

    for root, dirs, files in os.walk(root_dir):
        for filename in files:
            if filename.lower().endswith(".png"):
                full_path = os.path.join(root, filename)
                mtime = os.path.getmtime(full_path)

                # Check if cached
                if full_path in cache_dict:
                    cached_mtime = cache_dict[full_path].get("mtime", 0)
                    # If mod time unchanged, use cached tags
                    if abs(cached_mtime - mtime) < 1e-9:
                        tags = cache_dict[full_path]["tags"]
                    else:
                        # Re-parse
                        tags = parse_and_store_tags(full_path, mtime, cache_dict)
                else:
                    # Not in cache, parse fresh
                    tags = parse_and_store_tags(full_path, mtime, cache_dict)

                images_data.append({
                    "path": full_path,
                    "tags": tags
                })
                for t in tags:
                    all_tags.add(t)

    # Save updated cache
    save_cache(cache_dict)

    return images_data, sorted(list(all_tags))

def parse_and_store_tags(full_path, mtime, cache_dict):
    """
    Helper to parse the positive prompt from the PNG, clean it, tokenize, 
    store in the cache dict, and return the list of tags.
    """
    try:
        img_png = PngImagePlugin.PngImageFile(full_path)
        metadata_text = ""
        if hasattr(img_png, "text"):
            # Automatic1111 might store in "parameters" or multiple keys.
            # We'll just combine them all for demonstration.
            for val in img_png.text.values():
                metadata_text += val + "\n"

        # Extract the portion before 'Negative prompt:'
        positive_part = parse_positive_prompt(metadata_text)
        # Clean out parentheses and :1.6 type text
        cleaned_str = clean_prompt_text(positive_part)
        # Tokenize into tags
        tags = tokenize_prompt(cleaned_str)
    except Exception as e:
        print(f"Error reading {full_path} metadata: {e}")
        tags = []

    # Update the cache
    cache_dict[full_path] = {
        "tags": tags,
        "mtime": mtime
    }
    return tags


#####################################
# STREAMLIT APP
#####################################
def main():
    st.set_page_config(page_title="Booru Image Browser", layout="wide")

    # Minimal custom CSS for dark mode + ensuring left sidebar scroll
    dark_css = """
    <style>
    /* Make app background dark */
    body, .css-18e3th9, .css-1outpf7 {
        background-color: #2b2b2b !important;
        color: #e0e0e0 !important;
    }
    /* Left sidebar scrollable */
    section[data-testid="stSidebar"] .css-1d391kg {
        background-color: #3b3b3b !important;
        overflow-y: auto !important;
        height: 100vh !important;
    }
    /* Multiselect, text inputs, etc. - dark background */
    .stTextInput, .stTextArea, .stSelectbox, .stTagsInput, .stMultiSelect {
        background-color: #4b4b4b !important;
        color: #ffffff !important;
    }
    /* Checkboxes text color */
    .stCheckbox, .css-1e5imcs, .css-1d9dxig {
        color: #ffffff !important;
    }
    /* Primary buttons style */
    button[kind="primary"] {
        background-color: #666666 !important;
        color: #ffffff !important;
        border: none;
    }
    /* We avoid any "cursor: not-allowed" rules to ensure input is clickable */
    </style>
    """
    st.markdown(dark_css, unsafe_allow_html=True)

    # Load images from disk and cache
    images_data, all_tags = load_images_and_tags(ROOT_IMAGE_DIR)

    # Session state for favorites
    if "favorite_tags" not in st.session_state:
        # We'll store single tags and also allow combination tags (comma-joined).
        st.session_state["favorite_tags"] = set()

    # Session state for selected image
    if "view_image" not in st.session_state:
        st.session_state["view_image"] = None

    # Title
    st.title("Booru Image Browser")

    #################################
    # LEFT SIDEBAR: TAGS + FAVORITES
    #################################
    st.sidebar.header("All Tags (Scrollable)")
    st.sidebar.write("Check the box next to a tag to mark as a favorite.")

    # We do a single pass of all tags, but favorites appear on top.
    # The user also wants to store "tag combinations" as favorites. 
    # For simplicity, we’ll store single tags. 
    # If you truly need multi-tag combos as favorites, you can handle that differently.
    favorite_tags = sorted([t for t in all_tags if t in st.session_state["favorite_tags"]])
    nonfavorite_tags = sorted([t for t in all_tags if t not in st.session_state["favorite_tags"]])

    def render_tag_checkbox(tag):
        is_fav = tag in st.session_state["favorite_tags"]
        new_val = st.sidebar.checkbox(tag, value=is_fav, key=f"tag_cb_{tag}")
        if new_val and not is_fav:
            st.session_state["favorite_tags"].add(tag)
        elif (not new_val) and is_fav:
            st.session_state["favorite_tags"].remove(tag)

    # Favorites first
    for t in favorite_tags:
        render_tag_checkbox(t)
    st.sidebar.markdown("---")
    # Then non-favorites
    for t in nonfavorite_tags:
        render_tag_checkbox(t)

    #################################
    # SEARCH BAR (MULTISELECT)
    #################################
    selected_tags = st.multiselect(
        "Search or Add Tag (start typing for suggestions)",
        all_tags,
        default=[],
        help="Type to see matching tags in a dropdown, then select."
    )

    # Filter the images by selected tags
    if selected_tags:
        selected_set = set(selected_tags)
        filtered_images = [img for img in images_data if selected_set.issubset(img["tags"])]
    else:
        filtered_images = images_data

    #################################
    # MAIN AREA: THUMBNAILS OR FULL
    #################################
    if st.session_state["view_image"] is not None:
        # Show full-size image
        st.image(st.session_state["view_image"], use_container_width=True)
        if st.button("Back to Thumbnails"):
            st.session_state["view_image"] = None
    else:
        # Show thumbnail grid
        num_cols = 3
        for row_i in range(0, len(filtered_images), num_cols):
            row_imgs = filtered_images[row_i : row_i + num_cols]
            cols = st.columns(num_cols)
            for col_i, img_info in enumerate(row_imgs):
                with cols[col_i]:
                    st.image(img_info["path"], use_container_width=True)
                    btn_label = f"View {Path(img_info['path']).name}"
                    if st.button(btn_label, key=img_info["path"]):
                        st.session_state["view_image"] = img_info["path"]


#####################################
# ENTRY POINT
#####################################
if __name__ == "__main__":
    main()
