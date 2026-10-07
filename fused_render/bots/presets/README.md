# Bot presets

One folder per preset. `preset.json` names it (`name`, `color`, `order`, `model`,
`instructions`); every `*.md` beside it is a playbook in the Skills format
(`# title`, a `trigger:` line, numbered steps) that is copied into the new bot's
skills folder when the bot is created from the preset. The folder name is the
preset key and the brand icon drawn on the avatar (see BRANDS in src/core.js).

Optional `apps`: a list of starter keys (`starters/<key>`, see installapp.py) that
apply_preset installs into the apps folder when they are missing, so a bot made
from the preset has those app tools on its first task.

Optional `setup`: a task the new bot runs by itself right after its greeting
(bot.create → greet). The site presets (x, instagram, linkedin, tiktok, facebook,
reddit, youtube) use it to open the site's sign-in page and pop the `login`
window, so the user is asked to log in the moment the bot exists instead of on
its first real task. A task the user typed during the greeting wins and the
setup is dropped. Use the site's own sign-in URL, not its home page: a
signed-in profile is redirected to the feed, so the bot sees it is already
logged in without being told.
