# Melvor Idle 2 helper bot

Screen-reading click bot for Melvor Idle 2 (Godot 4 / C#, Steam, Windows). It keeps the
character busy: follows a rotation of skills, picks the best unlocked option, smelts ore
into bars by rule. No game files are modified.

## Run

- GUI: `start_gui.cmd` (or `.venv\Scripts\pythonw -m bot.gui`)
- CLI: `.venv\Scripts\python -m bot.main [--dry-run] [--ticks N] [-v]`
- Read-only probe against the live game: `.venv\Scripts\python tools\probe.py`
- Hotkeys (global): Ctrl+Alt+P pause/resume, Ctrl+Alt+Q stop.
- Settings live in `config.json` (the GUI edits it; gitignored). `config.default.json` is the shipped
  copy, made into `config.json` when that is missing. Refresh it from `config.json` when defaults change.
- Release: `MelvorBot.exe` (PyInstaller onefile, `tools/build.py`, entry `melvor_bot.py`). A pushed
  tag `v*` builds it on GitHub Actions (`.github/workflows/release.yml`) and publishes the exe and
  `SHA256SUMS.txt`. The tag is the version (`bot/_build.py`, written by the build, not in git).
  `--collect-all winrt` is needed: the WinRT projections are namespace packages.
- The exe installs itself (`bot/install.py`): started from elsewhere it asks, `--install` does it
  quietly. Program in `%LOCALAPPDATA%\Programs\MelvorBot`, data (config, logs, state) in
  `%LOCALAPPDATA%\MelvorBot` (`bot/paths.py`: `ROOT` is the data folder, `RESOURCES` the bundled
  files), a Start menu shortcut and an entry in Windows' app list (`--uninstall` keeps the data).
- Self-update (`bot/update.py`, only the installed exe): a minute after start and every
  `self_update.every_minutes` (60) it reads the latest GitHub release (public repo, no token),
  downloads the exe, checks SHA256SUMS.txt, and the window installs it when no fight or Thieving runs
  and the user is not playing: stop the bot, rename the running exe to `.exe.old` (allowed while it
  runs), move the new one in, start it with `--updated-from <old> [--start]`, exit.
  Every start of another copy goes through `update.spawn_independent` (`PYINSTALLER_RESET_ENVIRONMENT=1`):
  otherwise the onefile child reuses the parent's `_MEI` unpack folder, which the parent deletes on
  exit (0.2.1 crashed on its first start with "cannot import name '_imagingtk'").
- Processes started from the Claude desktop app (Bash, PowerShell) may see a private copy of
  AppData; start the bot or the exe with `Invoke-CimMethod Win32_Process Create` so files and
  shortcuts land in the real profile. Windows the exe opens can hide behind other windows: never
  click them blind on the user's screen, use `--install` for tests.

## Layout

- `bot/window.py`: find the window (title `Melvor2`), capture with `PrintWindow`, input with
  `PostMessage`. The real mouse never moves; the window may be covered but not minimized.
- `bot/ocr.py`: Windows built-in OCR (WinRT) on numpy images.
- `bot/game.py`: game actions built on OCR text: status bar, page navigation, card grids,
  fishing, smelting.
- `bot/main.py`: `Bot` loop (rotation, rules), `Controls`, `Hotkeys`, CLI.
- `bot/gui.py`: tkinter control window. Everything is English, the window texts too. Two columns:
  controls and the status box left, a notebook right (journal, shop, ores, settings in sub-tabs,
  technical log). Checkboxes, tabs and the order arrows are drawn with PIL in `_draw_images`
  (the clam defaults looked dated, arrow characters came out as coloured emoji). Minimum size
  1280 x 780: below that the settings tabs and fields fall off the right edge.

## Pitfalls

- The UI scale is **measured**, not derived from the window width: `Game.ui` takes it from the
  pitch of the skill rows in the sidebar (`SIDEBAR_PITCH`). At 2426 px wide the real scale is
  0.866, not 1. Every pixel offset is in reference px and goes through `Game.px()`;
  `Game.ocr_scale()` enlarges OCR on small scales. Offsets written before the scale was measured
  were real px at 0.866: the shop card window was too small for that reason and missed
  "Hold to Buy". Below ~1800 px wide OCR gets unreliable.
- `dry_run` skips only clicks that change the character's action (`click(..., action=True)`);
  navigation clicks still happen.
- Small white digits over artwork (ore counts) need `read_white_text` (threshold + upscale).
  A lone small digit (bait count) needs `read_small_number`: OCR skips a bare "0".
- One OCR pass over a full page drops lines at random (a whole row of cards once). Card grids
  use `read_union` (several scales + bands). Do not go back to a single `read` there.
- Never accept "the first match above 0.8": "Oak Tree"/"Teak Tree" score 0.82 and
  "Start Fishing"/"Stop Fishing" exactly 0.8. Take the best match, and use `find_vs` for
  start/stop pairs.
- Pointer parking (`park_mouse`) must stay on the empty strip left of "Bank". Further right the
  Quests button shows a "Locked" tooltip that covers half the sidebar.
- The status bar shows "Farming" while the Farming page is open (`PAGE_SKILLS`); the bot
  always leaves that page after tending the farm.
- Card grids differ per skill (`GRIDS` / `GridSpec` in `bot/game.py`): Mining has
  "Start Mining" / "Stop" on the card; Woodcutting has "Cut" on every card and shows the
  running tree in the side panel ("Stop Cutting"); Astrology's "Select" opens a detail view
  where "Study" starts it and "Change Constellation" goes back.
- Targets: "best" = last unlocked card in reading order (locked cards have no button, so
  this is the level check); "best Tree" / "best Ore Vein" filter on type (`Card.kind`, or the
  type's first word in the name); "least" = lowest `GridSpec.stat` (Astrology knowledge);
  "random"; or a name.
- The bot checks the target at start, on a level-up of the skill (status bar level) and every
  `reselect_minutes`. Fishing re-surveys on a level-up and moves only for a new best spot.
- Rotation `minutes` is a number, `[min, max]` (random per run) or 0 (stay). On the first tick
  the bot continues what the game is already doing if that skill is in the rotation.
- Clicks land on a random point inside the button's text box; waits are jittered.
- Fishing: spot xp comes from the hover tooltip of each location card (`survey_fishing`). The
  spot dropdown in the right panel is found by its label "Select Spot:" (`_current_spot_line`); the
  panel has two "Output" headers, and anchoring on "Output" once clicked "Special Items" for the
  list, so no spot was ever selected.
  The bait list cannot be clicked in the background: it closes on the press without selecting
  (hover works). The bot only reads the selected bait and its count.
- Food chain: Fishing gives raw fish, Cooking turns it into food (`survey_cooking` reads raw
  count, healing and cooked stock per recipe; the bot cooks the best-healing recipe it has
  ingredients for), Thieving spends it. `Bot.food` is the stock the GUI shows.
- Food is equipped per activity (Combat / Agility / Thieving) in the Bank: search the item,
  select it, press "Equip" under that activity (`equip_food`). The same panel holds the Sell
  controls: only ever click the "Equip" line found under the slot header and above "Sell Item".
- Typing works with posted `WM_CHAR` (`GameWindow.type_text`), e.g. the bank search box.
- Thieving has its own layout (`thieving_targets`): the button stays "Start" while running
  (an "Active" label appears), so the running NPC comes from the status bar. HP and the
  equipped food ("+53 HP [chest] 65") are in the page header (`read_vitals`); the bot stays on
  the Thieving page, eats below `eat_below` % HP and leaves when the food is gone.
- Cooking gates fish: a recipe unlocks at a Cooking level, so raw fish can pile up uncooked.
  `survey_fish` reads which fish every spot gives and the pieces in the bank (slow, about 100 s:
  cached in `Bot.fish_table`, refreshed every 3 h or when a new spot appears). A fish with no
  unlocked recipe and `backlog_limit` or more pieces is "waiting" (`waiting_fish`): its spots
  count for little in `choose_spot`, spots with cookable fish count double, and Cooking runs
  until its ingredients are gone. Skill levels come from the sidebar (`skill_level`).
- Firemaking: the three recipe categories are icons without text; logs is the first, clicked by
  position left of the list (`survey_firemaking`). Recipes are in unlock order; the bot burns the
  highest one it has logs for and stops when they run out. Tested live with Normal Log.
- Recipe pages (Fletching, Runecrafting, Crafting, Herblore; `RECIPE_SKILLS`) share Cooking's
  frame: `survey_recipes` / `create_recipe`. Only the first ingredient's have/need is read, so
  `start_recipe_skill` checks the status bar after Create and tries the next recipe if nothing
  started. A target like "level 5" (or `until_level`) makes the activity skip once reached.
- A rotation step with `"only_while_below": ["Fletching", 5]` is a helper for another skill's
  goal and is skipped once that level is reached. A level is the wrong trigger for supplies:
  Fletching still runs on Normal Logs (Arrow Shaft, Normal Bow) at level 11, so the Normal Tree
  step ended at 5 and Fletching starved. That step now uses `"only_while_stock_below": ["Normal
  Log", 300]` (`Bot.known_count`: the stock list, which the Firemaking page also fills; unknown
  means run).
- `"every_hours": 4` on a rotation step makes it low priority: it starts at most once per that
  many hours (`Bot.step_runs`, kept in the state file) and is skipped otherwise. The bush step
  (for Herblore later) runs so, 3-5 min.
- Counts on recipe cards: one OCR read is not enough ("509 / 1" came back as "509M");
  `_count_below` takes the first of several reads that parses.
- Page check is `on_page` (best matching line in the top bar); the joined title text fails for
  long names because the game title sits right above them.
- Bank guard: with `bank.block_free` or fewer free slots (`Bot.bank_full`) only `SAFE_WHEN_FULL`
  skills run, card skills only on a card done before (`known_targets`, no upgrades) and Cooking
  only food already in stock. A new kind of item is lost when the bank is full.
- Rotation steps can be switched off (`"enabled": false`, a checkbox per row in the GUI).
- Firemaking leaves `firemaking.keep` logs of every kind: the game burns until the logs are gone,
  so the bot caps the activity time at (logs - keep) x seconds per log.
- Hold buttons ("Hold to Water", "Hold to Open") use `Game.hold` (button down, wait, up).
  `open_items` opens containers in the bank (Bird Nest, Bait Box) through the "Open Item" block.
- Text heights shrink with the window: a fixed minimum height silently dropped recipe and spot
  names at 2426 px wide. Keep such thresholds low (`px(11)` to `px(13)`) and rely on structure.
- Combat is an activity (`start_combat`, `tend_fight`): any combat skill in the sidebar opens the
  page "Combat"; the status bar then reads "Combat <monster>". Stance decides what is trained
  and depends on the weapon (with a 2H sword: Slash = Attack + Strength, Crush = Strength,
  Block = Defence, all with Hitpoints). The tooltip that said so no longer shows on a posted
  hover, so `combat.stance` "keep" (default) leaves the stance the user set; a named stance is clicked. HP is the
  bar top left, in a band measured in px from the top (not a fraction of the height); never
  read left of 0.11 w, sidebar levels like "15/99" look like HP. The bot runs from combat before
  it switches activity and when the food is gone.
- Slayer: the Combat page has a "Slayer Task" block (master, monster, "12 / 27") with "Jump To Enemy",
  which starts the fight with the task's monster (`slayer_task`, `jump_to_enemy`). With
  `combat.mode` "slayer" (default) the bot follows the task and jumps again when the monster
  changes; "fixed" uses `combat.area` / `combat.monster`, which is also the fallback.
- A fight is never left alone. A Slayer monster (Scarecrow) took about 4 HP a second: the start-up
  trips to the smithy, farm and bank brought HP from 125 to 58 of 170. So while fighting the tick
  skips the smelting and farm checks, looks again within about a second, eats below
  max(eat_below %, 2.2 x the worst HP drop between two looks) and runs when HP is still under a
  quarter after eating. Stop a running fight (`run_from_combat`) before restarting the bot by hand.
- `open_combat` also accepts the "Run from Combat" button as proof of the page: the title is not
  always read during a fight. One status-bar read without "Combat" is checked a second time
  before the bot believes the fight is over.
- Shop costs are chips: icon, a coloured "have", a white "/ need". OCR splits the two numbers
  and reads the slash as a 1, so `cost_chips` finds the dark chip boxes and `read_cost_chip`
  cuts each number out by colour and reads it inside a rendered sentence (`sentence_read`). The
  enlarged strip read is only the fallback. `Bot.learn_prices` votes over reads (prices never
  change); the gold owned comes from the reads that agree with the top bar. Materials are icons
  without names.
- Buying (`Bot.read_shop`, `Game.buy_upgrade`): bank slots first, then the cheapest upgrade in
  reach, never below `shop.keep_gold`. Cards with the same name ("+25 Bank Slots") are told apart
  by their level requirement.
- Selling (`sell_candidates`, `sell_for`, `Game.sell_item`): only what the bot gathers and makes
  (logs, ores, bars, cheap food), never loot, never the reserves in `sell.*`, never the best foods
  or equipped food. It appraises first and sells only when the surplus covers the whole gap, at
  most one round per look at the shop. `sell_item` types the amount, checks that the game shows
  the same number and only then holds "Hold to Sell Items".
- The Shop's category list scrolls and keeps its position (Woodcutting is below the fold):
  `_shop_category` scrolls it both ways before giving up.
- Bank search: the box resets when the Bank is opened from another page, not when "Bank" is
  clicked while already there. The lone "x" that clears it is not always read by OCR;
  `_clear_bank_search` then uses the remembered spot or Backspace in the box.
- Armour (`Bot.check_armour`, `Game.gear_panel`, `equip_gear`, `smith_one`): per piece (Helmet,
  Platebody, Platelegs, Gloves, Boots) the best normal tier the Defence level allows
  (`ARMOUR_TIERS`; levels beyond Iron are guesses, the bank panel's own requirement decides). A
  piece in the bank is put on ("Equip To" in the Equip Item block, Combat loadout); a missing one
  is smithed once (Create, then Stop after the first) and put on; lower normal tiers are sold,
  better ones kept. Runs when Defence reaches a new tier or every 6 hours (`armour.every_hours`);
  one run is about three minutes of bank work. Weapons are not handled yet.
- Skill gear: every skill has its own Equipment Loadout (helmet icon at the right edge of a skill
  page; the panel opens when the pointer comes from elsewhere, slots show no tooltip and a
  background click on a slot only closes it). Gear with a skill bonus is a "Toolbelt" item and is
  equipped from the bank: the block "Equip to Toolbelt" lists "Equip To: <Skill> Utility <n>" with
  "Will Replace: <item>" (taken), "Achieve <...>" (locked) or nothing (free). `toolbelt_items`
  finds them by typing "Utility" in the bank search, which also matches type and description
  lines. A worn item leaves a placeholder with 0 pieces, so the stack is read first ("Sell 0 for").
  Only a free slot is filled; the click itself was never seen live (no spare item in the bank).
  "Equip Item" on ordinary Equipment points at the Combat loadout.
- Journal: `Bot.event(kind, text)` adds a line the GUI shows (kept in the state file). Levels come
  from the whole sidebar (`skill_levels`, at every farm check) and from the status bar; a level
  that drops or jumps more than two is believed only when read twice. Gold comes from the top bar.
  What came in is followed through the item notice bottom right (`read_drop`, "+1 Maple Log" over
  "774 in Bank"): `track_drop` keeps the first and last count per item and `flush_drops` writes one
  line per activity. A count that drops or jumps by more than 200 restarts the baseline, so a
  misread never inflates the gain.
- The time of the last look at the Shop is kept in the state file: a restart must not trigger
  another look and with it another round of selling.
- Duplicates (`sell.duplicates`, `Bot.sell_duplicates`): sold on their own about once an hour, not
  only when gold is short. One piece stays, and the worn one counts: the bank panel of a worn item
  says "Will Replace: <itself>" in its Equip block (`worn_in_panel`), and then the whole stack goes.
- Toolbelt slots are not only "<Skill> Utility": a Balaclava sits in "Thieving Helmet". The gear
  check searches the bank for every word in `Game.TOOLBELT_WORDS`.
- When a plot is ready the sidebar shows a red "!" right of the Farming level. `Game.farm_ready`
  looks for it (one OCR of the sidebar strip, about 0.6 s) twice a minute, and the farm is tended
  then; otherwise only every `farming.check_minutes` (60) for watering and plot prices. The other
  errands (bank items, gear, ranch, shop) run on `errand_minutes` (10), apart from the farm.
- Farming has tabs (`FARM_TABS`: Allotments, Herbs, Compost Bins). `tend_farm` visits each:
  harvest, plant ("Plant" or "Begin Compost"), water, and the price of the next plot for sale
  ("Purchase to Unlock"). Those plots are upgrades like the Shop's: `read_shop` adds them to the
  list ("Allotment plot", "Herb plot") and `buy_plot(tab)` buys one.
- The product picker of a plot ("Product: Select", a grid of icons) ignores background input: a
  posted click selects nothing and a posted hover shows no tooltip. `Game.choose_crop` uses the
  real mouse (below): Select sits `PICKER_SELECT_DX` right of the "Product:" label, the grid opens
  under it (`PICKER_GRID`, 4 wide; clicking an icon selects it and closes the grid). The tooltip
  gives name, seed needed / owned and the crop in the bank; a locked crop shows only "Farming Level
  N", and crops unlock in grid order, so the scan stops there. Empty plots get the crop with the
  smallest stock among those with enough seeds (stock rotation); nothing plantable keeps the plot
  as it is. Only the three rows without scrolling are read.
  The tooltip's title is the line right above "Input" (within 110 px of the seed row): the plot's
  own label ("Potato Fertiliser") sits higher in the read box and was taken for the title. Live
  checked 2026-11-20 at UI scale 0.99: Potato 116/3 seeds, 1669 in the bank; Onion 99/3, 280.
- Bait: the dropdown under "Select Bait" opens a row of stack tiles (posted click opens it, but a
  posted click on a tile selects nothing and posted hover shows no tooltip). Each tile has a muted
  blue badge with its count; the selected tile has a green background (`Game._bait_tiles`).
  `_pick_bait` (real mouse) always takes the biggest stack, which spreads the use over all baits;
  the bot does this when a fishing run starts and every `fishing.bait_minutes` (15) while it
  fishes, only while the user is away. An open bait list covers the label; a posted click on the
  open water closes it (and nothing else in the fishing scene reacts to it). Bait Boxes are opened
  with the Bird Nests (`open_items.names`).
- Thieving locations: "Select Location" opens a panel of location cards, each with a "Select"
  button; both work with posted clicks (a second click on the header closes the panel, so
  `thieving_locations` first checks whether it is open). The current location is the small grey
  text right of the header (read with `read_union`; one pass gave "LOMO'.nm District").
  `Bot.best_thieving_location` reads every location every 3 h or after a Thieving level-up
  (`thief_survey` in the state file) and goes where the best NPC with at least `min_success` gives
  the most xp per second (`ThiefTarget.rate` / `Bot.thief_rate`: "+13" xp under the portrait on
  success only, "3.00s" per attempt plus `stun_seconds` (3, a guess) after a failure).
  `min_success` was 90: only Thug (100%, +9) passed and the bot never left Lowtown; it is 50 now
  (the user prefers xp over food; backup eating covers the failures).
  Only the NPC column is read, without scrolling (a 4th NPC is below the fold).
- Thieving safety: a header read once gave HP unreadable and food 0 with 298 equipped; the bot then
  sat at 22/180 HP without eating and left with "out of food". Now: HP is read up to three times,
  an unreadable HP after a low one (or three in a row) eats blind, "out of food" needs three zero
  reads in a row, and while the user plays `emergency_eat` still eats on the Thieving page
  (no navigation). A farm badge that leads to nothing is ignored for 30 min, so errands stop
  pulling the bot off the Thieving page every minute.
- Eating: the game has its own auto-eat. The own HP bar (Combat and Thieving) carries a white line
  where auto-eat fires (shrimp icon) and an orange one at the opponent's max hit (sword icon; parked
  at the left end when there is none). `Game.hp_markers` reads both; the bar's ends are measured at
  full HP (red fill spans it all) and cached per page in `bar_extent`, because the empty part is
  dark navy and has no clean edge. The bot eats only as a backup (`Bot.backup_eat_limit`): below
  the auto-eat line or within 1.1x the max hit, at most twice per look; it runs only when HP is
  within one max hit after that. The old rule (2.2x the worst drop between looks) ate 72 pieces in
  nine minutes. HP text "145/270" in the top bar is the bank count, not HP (cy above 80 px).
- Auto Eat upgrades (Shop > Combat Upgrades: Auto Eat 2 needs Hitpoints 40 and 500K GP) only
  matter when an opponent's max hit reaches the auto-eat line. `Bot.note_auto_eat_short` records
  that (journal warning, `auto_eat_short` in the state file); for a week after, `read_shop` also
  reads Combat Upgrades (keeping only the Auto Eat cards) and `shop_rank` buys them right after
  bank space.
- Farming page OCR: the white "Plant" on its bright green button is missed by page-wide OCR at
  every scale (the plots stood empty for an hour). `Game.green_buttons` finds green buttons by
  colour (cv2 connected components) and reads each crop on its own; `_farm_lines` adds them.
- Compost: unlike the product picker, the compost picker ("Compost: Select") takes posted clicks
  and hovers. Before planting, `_tend_plots` presses "Apply Compost" (above Plant); when the plot
  says "No Compost selected" or "Not enough", `choose_compost` tries the tiles in order
  (Fertiliser first, +30% crop; it comes from Ranching) until the warning is gone. Select sits
  `COMPOST_SELECT_DX` right of "Product:", tiles at `COMPOST_GRID`.
- Item notice names were cut ("Death", "Dea Rune", "Penril", "Normal"): OCR splits a long notice line
  into pieces. `read_drop` reads from 0.78 of the width and joins the pieces on the "+N" row. When a
  learned name is still not found, `item_info` takes the one bank result whose title starts with it.
  A name with "Potion" is kind gear (their "Consumable: Yes" made them food).
- New items: an item notice whose "N in Bank" is no more than the "+N" just gained is the first of
  its kind. `Bot.track_drop` queues it (`learn_queue`), and on the next errand round `learn_items`
  reads its bank panel (`Game.item_info`: description and a kind from `ITEM_KINDS`: container,
  compost, bait, gear, food, seed) into `item_catalog` and the journal ("Learned"). The existing
  routines then use it (compost tiles, biggest bait stack, food list, gear check, crop choice);
  containers have to be added to the open-items list. `catalog` is the Shop price memory, a
  different thing: do not reuse the name.
- Patch scripts: a `\b` inside a plain (non-raw) Python string in a patch script lands in the
  source as a backspace character (the pop-up filter and a regex silently matched nothing). Write
  patch scripts with raw strings, and scan for control characters after patching.
- Hunting: an animal needs a trap of its type (Wild Chicken and Wild Cow: Snare), made in Crafting >
  Hunting Traps (Basic Snare: 2 Stick from bushes, 3 String, 1 Stone, 2 s each).
  `Game.start_hunting` selects the last animal with a Select button, picks the first trap tile with
  traps under "Select Trap" (the warning reads "No Trap selected!" or "Not enough Traps in Bank")
  and presses Begin Hunting; all posted clicks. Out of traps, `Bot.start_hunting` makes
  `hunting.traps_goal` (40) with `Game.make_count` (Create, wait, Stop) and tries again.
  `Bot.start_hunting` tries the animals best first (Greentail Bird needs a Small Bird Catcher and
  Binoculars); the trap recipe is "Basic <trap type>" from the panel (`hunting_trap_type`). An
  animal that does not start is passed over for 3 h (`hunt_failed`). Trap tiles are centred and
  their number varies per trap type: `badge_tiles` finds them by the blue count badge (bait too).
  Traps are used up fast (about 96 Basic Snares an hour). Snares and catchers need String, made in
  Crafting > Materials & Gems from Flax (Flax Bush); two supply steps before Hunting cut Flax and
  make String while the bank has under 150 (`"only": true` limits a recipe step to its target).
  `make_count` keeps the recipe's inputs (`recipe_inputs`) and the bot notes them as stock.
- `Game.px()` multiplies by the tuned scale (0.866): constants are in reference units, i.e. frame
  pixels divided by 0.866. Measuring in frame pixels and passing them to `px()` lands 13% short:
  the compost Select was missed that way.
- Recipe counts can read "2K / 2": `_count_below` parses K and M with `parse_amount`.
- `_open_bank` clicks Bank again when the page did not come up (a lost click left Cooking open
  and the Combat food was never equipped).
- Firemaking stops at the reserve by looking (`Bot.tend_fire`, every 2 min while burning: the
  Firemaking page count, then Stop Burning), not only by the time estimate: the logs burn faster
  than the page's interval, and 734 Normal Logs went down to 37. The counts it reads also update
  the stock list, which the Normal Tree supply step goes by.
- Recipe counts of 1,000 and more show as "1K / 1": before the K parse, Runecrafting saw no rune
  recipe at all (Rune Essence 1.66K) and made wizard gear from Wool instead.
- Weapon: `check_armour` also handles the weapon by Attack level. `Bot.weapon_type` finds the type
  of the normal weapon worn ("2H Sword") once in the bank and keeps it a week (`weapon` in the
  state file); a weapon from elsewhere gives None and is left alone. A new Attack tier (10, 20 ...)
  triggers the check as a new Defence tier does.
- "2H" is the one digit in item names: recipe names (`_recipes_on_screen`) and bank titles
  (`item_title`) keep it ("Copper 2H Sword" was read as "Copper" and never found).
- Recipe lists scroll: `category_recipes` reads, scrolls 6 notches down, adds what appeared
  (`Recipe.below`) and scrolls back; `create_recipe` scrolls when a card is not on screen. The
  Platebody of every Gear category is below the fold.
- Bank search: when the placeholder is hidden by an old word, `_bank_matches` clicks the remembered
  box and clears it with Backspace instead of giving up.
- Farm timing: after a visit with nothing to do the badge is muted for a while (it once looked lit
  every minute without work; the cause is unknown, an empty herb plot does NOT keep it lit). That
  mute made the bot miss ripe potatoes by seconds. `_note_timers`
  reads the growing plots' time left on each tab (loose OCR, "h0h 21m 5": minutes are the anchor)
  into `next_ready`; the bot comes back then (`farm_ready_at`) and mutes the badge only until then.
- Seeds are not for sale: the Shop's Farming category has Farmhands, Pest Control, the Grain Mill,
  Wine and Scarecrows; Items & Materials only sand and water. Herb seeds come as drops (Hunting,
  Thieving, Combat, Bird Nests).
- Scarecrow (pests on the plots show as a red bug count): `Bot.check_scarecrow` every 3 h equips one
  from the bank on the Farming Toolbelt, or buys a stack in Shop > Farming > Farming Items &
  Materials (`Game.shop_item`: such cards have no "Buy limit", so `survey_shop` skips them; the
  "Jump to" icons bring the section up). The amounts follow the Buy Quantity at the top (the user
  keeps 100: 5,000 GP and 500 Normal Logs for 100 scarecrows). The bot buys only when the
  "Hold to Buy" button is green: the red "have" of a material chip is not always read. The Normal
  Tree supply step aims at 800 logs for this.
- Compost: before planting the bot presses Apply Compost with the plot's chosen compost; when that
  runs out it tries the other tiles, so Compost from the Compost Bin gets used once it is made.
- Starting the bot from an agent session: a process started with Start-Process belongs to the
  session and dies when the session restarts (the bot stopped silently, the window was gone).
  Start it detached: `Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments
  @{CommandLine='"<repo>\.venv\Scripts\pythonw.exe" -m bot.gui --start'; CurrentDirectory='<repo>'}`.
  Day to day the user runs the installed exe (`%LOCALAPPDATA%\Programs\MelvorBot\MelvorBot.exe
  --start`, data in `%LOCALAPPDATA%\MelvorBot`). To test from source, stop it first and copy its
  `config.json` and `logs\state.json` over; never let two bots run on the game at once.
- Top-bar buttons: click the icon, not the label (`Game._top_icon`). After the Cooking page had been
  read, a click on the word "Bank" was lost every time (the Combat food was never equipped); the
  icon above it always took the click. Same for "Shop".
- The HP bar ends (`bar_extent`) are kept in the state file and measured whenever HP is full on the
  Combat or Thieving page (also right before a fight); without them the bot fell back to eating
  below 50%.
- Thieving survey: a location that reads empty keeps its earlier reading (one empty Lowtown left
  no safe target and Thieving was skipped twice). "Already equipped" in the bank while the header
  read 0 food means the header was misread: Thieving goes on.
- Mining fetches Rune Essence at half weight: with essence and Iron Ore both at 0% of their goal,
  Iron won the tie all night and Runecrafting stood still.
- Hunting locations: the left column lists locations ("Unlocked: N / M"); `hunting_locations` and
  `go_hunting_location` switch with posted clicks. `Bot.start_hunting` surveys every unlocked
  location every 3 h or on a level-up (`hunt_survey`: animal -> middle of its "+54-162" xp range)
  and tries the animals best first (Wild Boar in Woodlands ~395 xp per catch on 2026-11-21).
  The status bar can lag a few seconds after Begin Hunting: `start_hunting` checks four times and
  also accepts the right-hand panel showing the animal with "Stop Hunting".
- Farm timers in their last minute show seconds only ("42s"): read too, or ripe crops wait.
- Sidebar red dots (`Game.badges`): Farming and Ranching. Ranching's dot came from maintenance
  (comfort 75%): `Game.tend_ranch` presses "Repair all" (costs a few Normal Logs) and "Pet All" when
  animals want pets. Land and animals are never bought (the user does that).
- Fireflies in a Jar: Toolbelt items (slot "<Skill> Utility", consumable, e.g. +5% Prayer Points
  in Firemaking, +8 Light for Combat). `Bot.check_fireflies` every 4 h: for each skill in
  `fireflies.skills` whose jar is neither worn nor in the bank, Molten Glass (Smithing > Smelting,
  1 Bucket of Sand each), Glass Jar (Crafting > Materials & Gems, 2 glass, Crafting 20), then
  `Game.upgrade_item("Firefly", ...)` ("Select Upgrade (4)" lists the variants, "Hold to
  Upgrade") and `equip_gear`. A Toolbelt item without a placeholder leaves the bank once worn, so a
  jar put on in the last 24 h counts as worn (`jars_on`). Combat's Utility slots are a Shop purchase
  (Slayer -> Combat Toolbelt): `gear_panel` skips "Equip To" lines with "Purchase"/"Achieve" under.
- Typing: a posted "(" never reaches the bank search ("Fireflies in a Jar Firemaking)" found
  nothing). `_bank_select` types the name without its bracketed part and picks the right result by
  title; `item_title` keeps "(Skill)" in names.
- `_clear_bank_search` reads the lone "x" in the same box as the search field (a narrower box missed
  it every time).
- Stance "balance" (config `combat.stance`): the stance that trains the lowest of Attack, Strength
  and Defence. The stance buttons sit under "Stance" at the bottom of the equipment panel, below
  the fold (`stance_buttons` scrolls); the current one is "Attack Stance  <name>" in Offensive
  Stats. What a stance trains comes from its tooltip, read with the real mouse while the user is
  away and kept per weapon type (`stance_map`); until then the melee convention (Slash/Crush
  Strength, Block Defence, Stab Attack). A 2H Sword has Slash, Crush and Block.
- Never press Escape in the game: on most pages it opens the game menu ("Save and Exit to Main
  Menu", "Save and Quit to Desktop"). The stuck watchdog used to press it; it now only saves the
  picture. `dismiss_popup` closes that menu with "Resume" only.
- Card stats: a lone "0" is never read by OCR. `_card_stat` reads the row's right end on its own
  and takes nothing found as 0 (new constellations at 0 knowledge were left out of "least").
  Astrology's five cards fill the width: the 70% side-panel split applies only to grids without a
  `stat` (Iridan, the fifth card, was taken for the panel).
- Recipe skills rotate: within the first category that has something to make, the recipe started
  longest ago goes first (`recipes_made`), then the most xp per make (`Recipe.xp`, the "+3" badge
  right of the card name; OCR reads the bar icon in front as "111"/"III"). Before, the highest
  recipe in reading order always won and Runecrafting made only Mind Runes.
- Combat respawn: after a kill the next monster takes a few seconds to spawn and the status bar is
  empty meanwhile. The tick looks again up to four times 2.5 s apart before it calls a fight over
  (it "restarted" a running Slayer fight every minute, with a warning each time).
- Shop currencies: gold is the cost chip whose "have" equals the gold in the bank (the same on every
  card); a chip in the thousands with another "have" is Slayer Coins, which selling cannot bring in,
  so it never triggers a sale. The Slayer category is read; "Complete <area> n / m times" on a card
  is a requirement (`entry["task"]`). Slayer Loadout needs 5,000 coins and Slayer 15.
- Herblore: potions need 1 Vial + 1 herb + a skill item. `Bot.supply_vials` (when Herblore has nothing
  ready, at most hourly): buy Bucket of Sand when short (Shop > Items & Materials, 100 for 500 GP;
  allowed by the user), smelt Molten Glass, craft Pack of Vials (one pack = 20 Vials, so
  `herblore.vial_packs` is 2). Potion names end in a tier code ("Fishing Potion P1-A", OCR "PI-A").
- `every_hours` applies only when a turn begins: a level-up re-check in the middle of the bush turn
  found the turn's own start "too recent" and ended it after a minute.
- Food counts on the Cooking page include food sitting in the skill slots: all Trout was in the
  Combat slot and the bank had none. Thieving, like Combat, tries the next food when one is not
  found in the bank.
- Shop buys: "Hold to Buy" needs about 4 s; at 2.5 s Slayer Loadout was "bought" eight times and
  never got. `buy_upgrade` checks afterwards (the card leaves or its buy limit changes) and a failed
  purchase rests 4 h (`shop_failed`). The journal names the currency (GP or coins).
- Status bar: the first word that reads as a skill is the skill ("Herblore | Fishing Potion P1-A");
  the best match anywhere let "Fishing" win and Herblore was restarted every minute.
- Potions are Toolbelt items (slot "<Skill> Potion"): "Potion" is in `TOOLBELT_WORDS`, and
  "Toolbelt" in a description makes the item kind gear. Their panel names no slot up top: the skill
  comes from "Equip To: <Skill> Potion". A potion is used up, so when its slot holds the same potion
  ("Will Replace: Woodcutting Potion P1-A (1)", compared by `_potion_base`) the bot presses All and
  Equip To to add the whole stack; any other item in a taken slot stays the user's call.
- Containers opened in the bank: `open_items.names`, `ALWAYS_OPEN` in `bot/main.py` (Small Survey
  Findings, from Hunting) and every learned item of kind "container".
- UI scale changes move things apart: Hunting animal names sit up to ~260 px (reference) left of
  their Select button at UI scale 0.90; at 160 no animal was found. Thieving reads the NPCs again
  when a fresh location shows none.
- Weapon: the remembered type is checked against what is worn on every armour check (`weapon`
  keeps type and tier). The user switched to an Iron Scimitar with an Iron Shield; a one-handed
  weapon adds "Shield" to the pieces that follow Defence.
- A real-mouse failure (SetCursorPos refused, screen just locked) used to abort the farm visit
  after the harvest and leave the crop picker open over the Plant buttons: the plots then stood
  empty for hours. `choose_crop` errors are caught, the plot keeps its crop, and the real mouse
  rests 10 min (`real_failed_at`). A crop not in the bank (stock unread) counts as stock 0.
- Farming has a Trees tab (Farming 15; a plot costs 100,000 GP). `shop.priority_plots` (Trees) rank
  right after bank space, and while one is unaffordable no lower-ranked upgrade is bought.
- Agility: course cards ("Climb 1": success %, interval, damage on fail, "+3 Skill XP") with
  "Start Course" (posted click works). `start_agility` takes the course with
  `agility.min_success` (100) and the most xp per second, so no HP is ever lost.
- Selling keeps what a supply step gathers up to that step's goal (`only_while_stock_below`): the
  Normal Tree step cut logs to 800 and the sale took them back down to `keep_logs` (250).
- Ranching land is an upgrade like a farm plot (`Game.ranch_land`, `buy_ranch_land`; the leftmost
  "Purchase Land" card, 35K / 125K / 250K on 2026-11-22). Animals are never bought (the user's
  rule). Enclosure upgrades are not automated yet.
- GP per minute: at the end of every activity `flush_drops` values what came in (pieces x known
  sale price) and keeps an average per activity key (`gp_rate`, "Woodcutting best Tree"). Inputs used
  up are not subtracted, so processing (Cooking, Smithing) reads high. Items without a price are
  queued (`price_queue`) and looked up in the bank (`appraise_item`, sells nothing), three per
  errand round. While saving for a priority item (`saving`, set by `read_shop`), `money_factor`
  makes turns of activities within 80% of the best GP/min 1.5x longer and those under 25% 0.7x.
- The game setting "Disable Press and Hold" (on since 2026-11-25) renames the buttons: "Open",
  "Sell Items", "Buy", "Water", "Upgrade" instead of "Hold to ...". `is_action_button` accepts both
  forms (the short one almost exactly: "Sell Item" is the block header, "Watered" a plot label);
  holding still works, the release is the click. `_sell_rows` takes the upper "Sell Item" line as
  the header, since the button "Sell Items" otherwise won and the sell block read empty.
- Opened with the nests: Bird Nest, Bait Box, Seaweed Clump, Eagle Nest (`open_items.names`).
- Selling without a gold shortage (`sell_spare`, hourly): `sell.always` (Junk) and cooked food below
  the best `keep_best_foods` that is not equipped. `sell_old_gear` (with the armour check) sells
  normal gear of every kind (weapons, shields, armour) from tiers below the best one both Attack
  and Defence allow; bank titles lose trailing icon scraps first ("Stone Mallet e").
- Coal: Steel, Mithril and better bars take Coal besides their ore, and only the ore was counted,
  so the smithy fell back to Iron Bars. `smeltable_ore` also wants Coal (`mining.coal_goal`, 400)
  once such a bar is unlocked, and the sales keep Coal up to that goal. Smelting rules now include
  Mithril Bar and Gold Bar (Mithril Ore was never smelted without a rule).
- Herb seeds: not sold anywhere. Thieving in Farmers Market: the Herbalist (55% at Thieving 47, max
  hit 100) drops Penril, Sourweed, Mantalyme, Nomel and Oxilyme Herb Seeds (15% common table) and
  Pack of Vials; the Market Seed Stall drops crop seeds, rarely herb and tree seeds; the Farmer
  drops crops, no seeds. "View Possible Rewards" on each card shows the table (posted click works;
  close with the "x" top right of the dialog).
  When the Herbs tab had plots without seeds on the last farm visit (`Game.seedless_tabs`), the
  Thieving turn goes to `thieving.herb_seeds` (Herbalist, Farmers Market, at least 45%) instead of
  the safest NPC; the usual backup eating and the HP watch cover the failures.
- Real mouse (`window.RealMouse`, `win.real_mouse()`): SendInput clicks and hovers with the game
  lifted to topmost; afterwards cursor, foreground window and the game's place at the bottom are
  restored. Only when `Bot.real_mouse_ok` says so: no user input for `real_mouse.idle_minutes`, the
  screen not locked, no app other than the game making sound (`desktop.may_use_real_mouse`).
  SendInput resets the system's last-input time like the user's own input, so
  `desktop.note_own_input` remembers the bot's tick and `desktop.user_idle_seconds` (also behind
  `GameWindow.user_idle_seconds`) leaves it out; without that the bot would yield to itself.
- Info pop-ups ("New Unlock! Quests", level up, "New Pet Located") dim the page and swallow every click until
  "Confirm" is pressed; one blocked the bot for 12 hours. `Game.dismiss_popup` closes them at the
  start of every tick and when a skill page does not open, but only with a known info title and
  never when the text mentions buying, selling or deleting. When pages keep failing, open_skill
  saves `logs/stuck.png`, presses Escape (harmless in the game) and the journal gets a warning.
- Right after containers are opened the bank header can be unreadable for a few seconds:
  `_open_bank` waits and saves `logs/bank_not_open.png` when it still fails.
- Turn length follows the skill level (`Bot.level_factor`): the configured minutes times
  sqrt(middle level of the rotation / this level), within 0.6 to 1.6; Combat counts as its lowest
  melee stat. Never below `min_turn_minutes` (8), unless the step itself is set shorter.
  Levels only take names from `SKILLS`: a misread status bar once added "Mloodcutting".
- "Next activity" in the window sets `Controls.skip`; the next tick ends the current turn.
- Tick order: status, the rotation's own step (turn over -> next; not doing the step -> start it),
  and only then the errands (smelting, farm, bank, gear, ranch, shop). Errands first made every
  restart spend a minute or two on them while the turn was over, and the window showed the next
  step while the game still did the last one. `Bot.task` says which errand runs, for the window.
- Mining target "smeltable" (`Bot.smeltable_ore`): the ore of a bar the Smelting page shows as
  unlocked, furthest below its smelting rule (`smelting.rules`, else `sell.keep_ores`); it stays on
  the running ore while that one is short. When all are stocked it falls back to "best Ore Vein".
  "best Ore Vein" alone mined Adamantite all day while Smithing could only make Copper, Iron and
  Silver bars. It also fetches Rune Essence while Runecrafting is in the rotation and has
  less than `runecrafting.essence_goal` (200); the stock is the first ingredient of the rune
  recipes as the last Runecrafting turn read it (`Bot.recipe_raw`). `ORE_FOR_BAR` maps the bars whose ore name differs (Steel needs Coal too, not read).
- Smelting prefers the best bar an ore makes (`BAR_ORDER`): a rule for Iron Bar first tries Steel
  Bar (Iron Ore + Coal) and falls back when it does not start. Recipe card counts are read soft
  first (`_count_below`): the hard-mask reads made "93 / 1" into "9331" and smelted far too early.
  A smelt that runs when the bot starts is left to finish, but only a bar or Molten Glass: once
  `smith_one` did not see its Stop button (UI scale changed to 0.81), Steel Boots went on using every
  Steel Bar and the "let it finish" wait held the rotation for an hour. `smith_one` now looks wider
  and checks the status bar until Smithing has stopped.
- Bank item titles can start with the icon read as a letter ("o Trout 66"); `Game.item_title`
  drops it. Without that "Trout" was never found and Thieving could not equip it.
- Slayer progress "23 / 27" often comes back as "23127": `slayer_task` reads that spot again with
  soft edges, in a band around the line's centre (a merged OCR box can sit half a line high).
- Recipes that do not start (a second ingredient missing) are left alone for 2 hours.
- Recipe pages: the category column reaches about 0.33 of the width; reading only to 0.27 cut
  "Unlocked: 1 / 10" in half, so Fletching was skipped every round ("nothing to make") and the
  helper step "Normal Tree while Fletching < 5" never ended. Recipe names can carry icon scraps
  ("0 Trout"): `Game._named` strips them before matching.
- The rotation survives a restart: the step, its start time and its length are in the state file
  (`cycle_state`), and `pick_start` carries on with the time that was left. Before this every
  restart gave the running activity a fresh full turn; a day with many restarts was woodcutting only.
- A minimized game window cannot be read. The bot puts it back without focus and behind the other
  windows (`GameWindow.restore`); `restore_window` switches that off, except during a fight.
- Opening a container ends in an "Item Opened" dialog that stays until "Confirm" and dims the page
  (the bank header was unreadable behind it). `confirm_item_opened` reads what it lists and
  confirms; `_open_bank` calls it too when the header is not found.
- Bank search: the game colours a tab with results orange, the open tab green, the rest stay
  plain. `_result_tabs` goes to the orange ones first and skips the plain ones (a lookup takes
  about 12 s instead of walking all eight tabs).
- The bot stands back while the user plays (`Bot.user_guard`, `GameWindow.user_idle_seconds`): the
  game is the window in front and the last real mouse or key input is younger than
  `yield_seconds`. Posted messages are not input to Windows, so the bot never triggers itself.
  The guard runs before every click and raises `UserActive`, which drops the current tick; the
  next one starts from a fresh read. A fight is the exception: the bot keeps eating.
  Leaving the game for another window used to free the bot at once (no game in front = not
  playing), so it jumped in the moment the user alt-tabbed. `Bot.watch_front` (a thread, every
  0.25 s) notes when the game was last in front with recent input; `user_guard` keeps standing
  back for `yield_after_leaving` (3) s after that. The state goes back to "running" right after
  the guard passes, before the tick, so the window never says "You are playing" while the bot
  works.
- Stock list (`Bot.inventory`, `note_stock`, `stock`): pieces and price per item, from the item
  notices, from every look at an item in the bank and from sales. Selling plans from it instead
  of appraising every item, and a listed duplicate with none in the bank is not looked up again
  until a notice shows it. There is no full scan of the bank; the orange tab already leads to an item.
- The user can change the game's UI scale and reorder the bank tabs at any time (seen: 0.87 to
  0.94 between two starts). At 0.94 card names, Combat HP, the Astrology page and prices read
  worse: every threshold was tuned at 0.866. So the bot no longer trusts its constants at other
  scales: `ScaledWindow` resizes every frame to `TUNED_SCALE` and maps click positions back
  (`Game.ui` measures the real scale on the raw frame every 10 minutes). Regions given as
  fractions of the window still assume about the tuned layout; they hold at 0.94, not checked further.
- Small numbers: `sentence_read` renders the glyphs with their soft edges. A hard mask of 9 px
  digits turned "40,000" into "4-0n"; the soft render read every price in the saved shop frames.
  `have_need` splits a cost text by colour, `soft_number` reads a white number at a known spot.
- With a larger UI scale the sidebar no longer fits and stays scrolled: `open_combat` scrolls it
  back up to find "Attack".
- Farming (`tend_farm`): every look at the plots is a `read_union`; a single read missed the
  "Plant" button for hours. "Not enough Compost" next to the button does not stop planting, only
  "Not enough Seeds" does.
- Ranching: only the stats bar is read (`ranch_stats`). The feeding and management tabs do not
  open on background clicks. The page reopens where it was left; inside one enclosure the land
  overview (Repair all, "N Animals want pets", Pet All) is out of sight and "Return to Land"
  ignores posted clicks too: `tend_ranch` presses it with the real mouse when allowed. Pet All had
  never been pressed before that. Hovering an animal shows its checklist (wants pets, treats,
  group bonus at 8/14/18 animals) and "Dead in 5d ..." (its life span).
- An activity that cannot run now (no food, nothing to cook) sets `Bot.skip_reason` and the
  rotation moves on at once; that is not a failure.
- Game updates: `bot/steam.py` reads the Steam app manifest (`buildid`, `TargetBuildID`,
  `StateFlags`) once an hour (`update_check`). A waiting update goes to the journal as a warning
  (Steam only applies it when the game restarts). A new build gets its patch notes from the public
  Steam news API in `logs/patchnotes/build-<id>.txt`: **read that file first after an update**,
  it lists renamed buttons, moved shop categories and changed tooltips. SteamDB blocks fetches.
- Game start (`Bot.ensure_game`, `steam.launch_game`): no game window and no `Melvor2.exe` -> the bot
  opens `steam://rungameid/3218350` (starts Steam too), at most every `game_launch.retry_minutes` (5).
  A window that stays unreadable for `restart_hung_minutes` (20) is closed (WM_CLOSE, then taskkill)
  and started again: on 2026-11-26 Windows ran out of memory at 06:01, dwm crashed and the game
  window gave "PrintWindow failed" for five hours. The game has two launch options (normal, Vulkan):
  Steam asks which one unless "Always use this option" is ticked (done 2026-11-26, the normal one).
- Autostart (`bot/autostart.py`): a "Melvor Bot" shortcut in the user's Startup folder runs
  `MelvorBot.exe --start` (from source: `pythonw -m bot.gui --start`); the checkbox in Settings >
  General creates or removes it (off by default, the user's choice). The GUI sets its own
  AppUserModelID so the taskbar shows the "M" icon instead of Python's.
- `save_settings` starts from the loaded config: keys without a field in the window (game_launch,
  update_check, mining, ...) were dropped on every Save before.
- After Load the game shows "Welcome Back! You were away for ..." (offline gains) and swallows every
  click until Confirm; `dismiss_popup` presses it (the title sits above the usual pop-up band). Before
  that, every skill page "could not open" and the rotation skipped six activities in a minute. After
  the window appears (`Bot.game_missing`) the bot polls `Game.start_screen` every 2-4 s (one quick
  OCR pass, ~0.4 s) and presses Load as soon as Character Selection shows, then the ticks begin.
- `Game.ui` remembers a failed scale measurement for 15 s: on screens without the sidebar (start
  screen, loading) every read measured again (two OCR passes), and Load came 1:39 min after the window.
- After a game restart the game opens on Character Selection. `dismiss_popup` clicks "Load" only
  when there is exactly one character; with more it leaves the choice to the user.
- v0.11.7 (build 26886380): stance tooltips also show the XP split ("Attack 50%"), item stats in
  tooltips need Shift (setting "Always show Item Stats"), the Agility Mark Shop is split per
  skill, Mastery Guild notifications queue one at a time, the loadout hotkey is now Q.
- Combat food budget: a Slayer task on Rainbow Slime ate the whole food stack in nine minutes
  (the bot ate 38 times, auto-eat more) and left 6 of 320 HP; with the food gone Thieving stopped
  too. `food_budget_spent` now leaves a fight above `max_food_per_min` (2, after 3 min) or at
  `food_reserve` (5) left, and `too_hard` keeps that monster out for `too_hard_hours` (24): the
  Slayer task then falls back to the configured monster, and the in-fight "jump to the task" skips it.
- The equipped-food count is read on every look during a fight: a jump up (new food equipped) counts
  only when read twice, and an impossible drop (over 200 or half the stack within 10 min) restarts the
  count. One misread "8000" made "ate 8000 food in 9 min" and Evil Plant, the fallback monster, was
  skipped for a day; `drop_misread_too_hard` forgets such marks when the state is loaded.
- Combat area list keeps its scroll position: after a Slayer area further down the fallback
  "Corrupted Farmlands" was out of view and combat failed three times in a row. `start_fight`
  now scrolls the list up and down before giving up and logs what it saw.
- Combat area popup with no area chosen opens on "No Category selected": `start_fight` clicks
  "Combat Areas" first.
- Bank "not found" logs the titles of the results it clicked (`bank_seen`). That showed the real
  cause of "Sardine/Pike/Mackerel not found": the price's coin icon reads as one letter after the
  name ("Sardine e", "Pike a"), which pulled `similar` under 0.9. `item_title` now drops trailing
  one-letter words.
- Real mouse: SetCursorPos fails with error 0 after the user has been away a while although
  `screen_locked()` says no (display off?); it works again once the user is back. The 600 s
  back-off in `real_mouse_ok` keeps it to one try per ~10 min. Cause not found yet.
- Food choice (`food_choices`): the food list from the Cooking page only grows, so Salmon and Pike
  stayed in it after they ran out and the bot tried them (and Mackerel, sitting in a slot) while
  Sardine 334 and Beef 629 waited; it gave up after three. Now up to six are tried, and a food the
  bank search does not find (`Game.bank_missing`) is passed over for an hour. The game's own
  "Select Food" picker (sorted by Healing, with stock) does not open on a posted click.
- `eat_up` eats to 60% of max HP before Thieving or Combat starts (HP sat at 6 of 320 after a fight).
- `sell_old_gear` only sells a piece when a better piece of the same kind is in the bank (or is the
  remembered worn weapon). "Allowed to wear Steel" alone sold Iron Boots, Platebody and a spare
  Iron Scimitar while no Steel pieces existed.
- Agility courses are spread over locations (Climbing Grounds, Jumping Grounds, Arenas, Unique
  Trails); the page shows only the last picked one. The location panel is the Thieving widget, so
  `start_agility` reuses `thieving_locations` / `go_thieving_location` and looks at all of them.
  Location names can come with an icon read as a letter ("Z Arenas"): stripped.
- `sell_old_gear` counts a better piece only with a stack ("Sell N for") or when worn: sold items
  stay in the bank as placeholders with 0.
- Fishing spot choice: a mixed spot counts a pile Cooking cannot use in part (Windy Bay = Mackerel +
  537 Cod), and while good food runs low (`food_goal` 300 pieces healing `good_heal` 150+) spots
  score `1 + heals / heal_scale` of their best cookable fish (Rocky Shores for Lobster). The random
  `variety` pick is off then: it sent the bot back to Windy Bay. The top four scores are logged.
- HP does not come back outside a fight: 6 of 320 stayed for hours. `eat_up` covers the next start.
- `create_recipe` scrolls down and then up: the recipe list keeps the scroll of the last survey,
  so the first card of a category ("Normal Bow") was out of sight above.
- GP per hour: the status bar shows the game's own figure right of the XP rate ("[chest + coins]
  +3,600.00/hr"), net of the inputs used up; item rates follow it. `Game.gp_per_hour` takes the
  signed rate with coin-coloured pixels just left of it (gold on most pages, light blue on
  Runecrafting). The bot keeps the median of a turn's readings as `gp_rate` (GP/min); its own
  loot x price estimate is only the fallback (Combat, or when the bar shows no GP).
- The bank counter top left ("223 / 295") was read as 773 once: a reading with used > total is
  thrown away.
- Quests (`Bot.check_quest`, every `quests.every_minutes` (20) in the errand round, not during
  Thieving or Combat): `Game.read_quest` opens Quests (the top-bar label is often only read inside
  "Bank Shop Quests Completion": `_top_label` cuts it out), takes the last unlocked step (later ones
  say "Progress further ..."), and when that step has a Give button reads "have / need" by colour
  (`have_need`, skipping the icon). The item has no name on the page: its tooltip shows only for the
  real mouse, hovered `px(42)` right of Give (`+22` was the button's edge). Enough -> Give; short ->
  `Bot.make_item`: `recipe_location` (an index of every recipe page, `recipe_index` in the state,
  rebuilt daily) -> `find_recipe_card` -> `recipe_io` (inputs with qty/have, output per make; a lone
  "0" have is missing from OCR and counts as 0) -> make what is short first, two levels deep ->
  `make_count`. Done live 2026-11-28: Stone -> 30 Stone Arrowtips (Smithing > Stone Gear) -> 24 Stone
  Arrows (Fletching > Arrows) -> Give. Steps without items (Slay 3 Archery Target with Ranged) only
  go to the journal.
- Quest count: Give, the item icon (width varies), a coloured "have" (red too few, green enough) and a
  white "/ need". The rightmost white run is "/ need" (a sword blade is white too), the coloured
  digits sit within ~7 px left of it, the icon ~10 px further: the colour decides Give (`enough`).
  The hover for the name happens once per quest step (`check_quest` keeps it), only with the real
  mouse allowed. Finished: "Quest Complete!" + "Claim Rewards" is pressed; "Rewards already claimed"
  -> the top open quest in the list is selected and "Start Quest" pressed.
- Quest fights ("Slay 0 / 3 Archery Target using Ranged Combat Style"): `Bot.quest_fight` sets the
  loadout (`Game.set_loadout`: header above Equipment, posted clicks; "Melee"/"Ranged"), puts arrows
  on (`ensure_ammo` -> `equip_ammo`: bank panel "Equip To: Ammo" after All), fights in the popup's
  "Quests" tab (an area per quest, "Act 1 - Pointy" / "Preparation" wrapped), waits
  `quests.fight_minutes` (4), runs, and restores the loadout. `start_fight` now always clicks its tab
  first: the popup reopens on the last tab, and after Quests "Corrupted Farmlands" was not in the list.
  With the Ranged loadout the melee stance logic is skipped.
- Goals (`goals` in config, e.g. "The Grain Mill": Farming, materials Oak Log, Stone, Molten Glass in
  the card's chip order): `Bot.check_goals` (every `goals_minutes`, 30) reads the card's chips via
  `shop_item`; what is short becomes `goal_needs`, which turns the Woodcutting target into "<Log>
  Tree" and the Mining target into the ore/Stone, Molten Glass is smelted from bought sand
  (`supply_glass`; how much sand one buy brings follows the Shop's Buy Quantity, the user's
  setting), the materials are never sold, and the item is bought once every chip is covered.
  `shop_item` scrolls in steps of 3 (5 skipped The Grain Mill), picks the card title with a "Costs"
  line under it (the section header has the same name), centres the chip crop on "Costs &
  Requirements" (titles are left-aligned), and a chip whose "/ need" was not read borrows the need
  the others agree on. "Jump to:" reads as "p to:" at UI scale 0.81.
- Agility picks the course with the most expected xp per second (xp x success / interval) at
  `agility.min_success` (80, was 100: only Climb 1 was ever run); below 100% food goes into the
  Agility slot so auto-eat covers the failed runs.
- Armour level requirements come from the Smithing recipe (`gear_level`, `gear_req` in the state):
  Steel 30, Mithril 40, Adamant 50 Defence, so the guesses were right. Missing pieces are made
  through the recipe chain (bars smelted from ore first).
- Bank tabs are placed by position, never by number: the user reordered them ("Tab 1, Tab 3, Tab 2")
  and the number-based spacing put "Tab 1" in the sidebar, so a cell click opened Combat.
- Recipe categories are found with `read_union`: a single read missed "Arrows" at UI scale 0.81, so
  Fletching never saw an arrow recipe.
- Tested live: Mining, Woodcutting (Cut), Astrology (Select + Study), Fishing spot choice,
  Farming harvest, plant and water, Cooking (Create), Firemaking, Fletching, Runecrafting,
  equipping food from the bank, Thieving with the Eat click, Combat, opening Bird Nests, buying
  in the shop and selling for an upgrade. Not seen live: the bank guard (the bank was never full).

## Backlog

The backlog is not in this repo. It lives in the Obsidian vault: the note with
`type: backlog` whose `repo:` field points to this checkout.
