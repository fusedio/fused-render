# Bot presets

One folder per preset. `preset.json` names it (`name`, `color`, `order`, `model`,
`instructions`); every `*.md` beside it is a playbook in the Skills format
(`# title`, a `trigger:` line, numbered steps) that is copied into the new bot's
skills folder when the bot is created from the preset. The folder name is the
preset key and the brand icon drawn on the avatar (see BRANDS in src/core.js).

Optional `apps`: a list of starter keys (`starters/<key>`, see installapp.py) that
apply_preset installs into the apps folder when they are missing, so a bot made
from the preset has those app tools on its first task.
