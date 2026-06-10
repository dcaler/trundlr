# trundlr Übersicht Widget

A macOS desktop status widget for [Übersicht](https://tracesof.net/uebersicht/) that displays your trundlr projects grouped by priority, with current task status and next scheduled task per project.

## Requirements

- macOS
- [Übersicht](https://tracesof.net/uebersicht/) — free download

## Setup

1. Install Übersicht from https://tracesof.net/uebersicht/
2. Open your widgets folder: **Übersicht menu → Open widgets folder**
3. Copy `Status.jsx` into that folder
4. Edit line 1 of `Status.jsx` and replace `YOUR_TRUNDLR_HOST` with your trundlr instance address:
   ```js
   const API = 'http://192.168.1.50:8251';
   ```
5. The widget appears on your desktop and refreshes every 2 minutes

## What it shows

- Projects grouped under **P1 / P2 / P3 / P4** priority headers
- Per project: the currently running or last completed task, plus the next scheduled task with ETA
- Fully completed projects are hidden automatically
- Clicking a project name opens trundlr's Projects view
- Clicking a task bullet opens trundlr's Tasks view

## Customisation

The widget ships as a minimal template — no logo or background image. To personalise it, add your own styles to the `className` block in `Status.jsx`. The conferences/travel section from the example in the docs is intentionally omitted; add any static sections you like above or below the `{blocks}` render call.
