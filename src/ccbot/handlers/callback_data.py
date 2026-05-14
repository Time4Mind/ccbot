"""Callback data constants for Telegram inline keyboards.

Defines all CB_* prefixes used for routing callback queries in the bot.
Each prefix identifies a specific action or navigation target.

Constants:
  - CB_HISTORY_*: History pagination
  - CB_DIR_*: Directory browser navigation
  - CB_WIN_*: Window picker (bind existing unbound window)
  - CB_SCREENSHOT_*: Screenshot refresh
  - CB_ASK_*: Interactive UI navigation (arrows, enter, esc)
  - CB_KEYS_PREFIX: Screenshot control keys (kb:<key_id>:<window>)
"""

# History pagination
CB_HISTORY_PREV = "hp:"  # history page older
CB_HISTORY_NEXT = "hn:"  # history page newer

# /help inline doc
CB_HLP_HOME = "hlp:home"  # back to top-level help screen
CB_HLP_SEC = "hlp:s:"  # hlp:s:<section_key>  open a help section

# Directory browser
CB_DIR_SELECT = "db:sel:"
CB_DIR_UP = "db:up"
CB_DIR_CONFIRM = "db:confirm"
CB_DIR_CANCEL = "db:cancel"
CB_DIR_PAGE = "db:page:"

# Window picker (bind existing unbound window)
CB_WIN_BIND = "wb:sel:"  # wb:sel:<index>
CB_WIN_NEW = "wb:new"  # proceed to directory browser
CB_WIN_CANCEL = "wb:cancel"

# Screenshot
CB_SCREENSHOT_REFRESH = "ss:ref:"

# Compact screenshot view (opened via the Shot button from main / /list)
CB_SHOT_SW = "sh:sw:"  # sh:sw:<sid>  switch active session + redraw screenshot
CB_SHOT_BACK = "sh:b:"  # sh:b:<m|l>   return to main / list view

# Interactive UI (aq: prefix kept for backward compatibility)
CB_ASK_UP = "aq:up:"  # aq:up:<window>
CB_ASK_DOWN = "aq:down:"  # aq:down:<window>
CB_ASK_LEFT = "aq:left:"  # aq:left:<window>
CB_ASK_RIGHT = "aq:right:"  # aq:right:<window>
CB_ASK_ESC = "aq:esc:"  # aq:esc:<window>
CB_ASK_ENTER = "aq:enter:"  # aq:enter:<window>
CB_ASK_SPACE = "aq:spc:"  # aq:spc:<window>
CB_ASK_TAB = "aq:tab:"  # aq:tab:<window>
CB_ASK_REFRESH = "aq:ref:"  # aq:ref:<window>

# Session picker (resume existing session)
CB_SESSION_SELECT = "rs:sel:"  # rs:sel:<index>
CB_SESSION_NEW = "rs:new"  # start a new session
CB_SESSION_CANCEL = "rs:cancel"  # cancel — drop the new flow entirely
CB_SESSION_BACK = "rs:back"  # back to directory browser at last selected path
CB_SESSION_PAGE = "rs:p:"  # rs:p:<page>  pagination

# Screenshot control keys
CB_KEYS_PREFIX = "kb:"  # kb:<key_id>:<window>

# A8 switcher
CB_SW_USE = "sw:"  # sw:<session.id>     — switch active session
CB_SW_NEW = "swn"  # open directory browser to create a new session
CB_SW_NOOP = "sw0"  # tap on already-active button (no-op)

# Footer (always under last bot message)
CB_FT_STOP = "ft:stop"  # send Escape to active session (busy state)
CB_FT_KILL = "ft:kill"  # confirm-archive active session (idle state)
CB_FT_CLEAR = "ft:clear"  # forward /clear to active session
CB_FT_MORE = "ft:more"  # open the Menu screen (pauses live-card updates)
CB_FT_TERM = "ft:term"  # open a native desktop terminal for the active session
CB_FT_OLDER = "ft:old"  # ◀ Older — page back into history from the live-card

# More menu
CB_MM_LIST = "mm:list"
CB_MM_STATUS = "mm:status"
CB_MM_SHOT = "mm:shot"
CB_MM_NEW = "mm:new"
CB_MM_ARCHIVE = "mm:arch"
CB_MM_SETTINGS = "mm:set"
CB_MM_BACK = "mm:back"  # back to default footer

# Settings (toggle screens)
CB_ST_GRP = "st:grp:"  # st:grp:<name>  open a per-group settings screen
CB_ST_LANG = "st:lng:"  # st:lng:<code>
CB_ST_PREV = "st:prev:"  # st:prev:<value>
CB_ST_LAG = "st:lag:"  # st:lag:<value>
CB_ST_VOICE = "st:voice:"  # st:voice:<value>
CB_ST_WDAY = "st:wday:"  # st:wday:<mon|tue|...|sun>
CB_ST_APPROVE = "st:apr:"  # st:apr:<off|webfetch|all>
CB_ST_TOK = "st:tok:"  # st:tok:<slot>:<+|->  bump session-token threshold
CB_ST_LOCAL = "st:local:"  # st:local:<off|on>  toggle native Terminal popup
CB_ST_LTERM = "st:lterm:"  # st:lterm:<emulator-name>  pick Linux template
CB_ST_LCLAUDE = "st:lcl:claude"  # send Linux Claude-fallback prompt to chat
CB_ST_CPOS = "st:cpos:"  # st:cpos:<push|delete|repost>  user-msg disposition
CB_ST_BACK = "st:back"  # back to Menu

# Confirmation dialogs (id-bearing variants take session.id)
CB_CONF_KILL_YES = "cn:kill:y:"  # cn:kill:y:<sid>
CB_CONF_KILL_NO = "cn:kill:n"
CB_CONF_DEL_YES = "cn:del:y:"  # cn:del:y:<sid>
CB_CONF_DEL_NO = "cn:del:n"
CB_CONF_DONE_YES = "cn:done:y:"  # cn:done:y:<sid>
CB_CONF_DONE_NO = "cn:done:n"

# Archive
CB_ARC_PAGE = "ar:p:"  # ar:p:<page>
CB_ARC_RESTORE = "ar:r:"  # ar:r:<session.id>
CB_ARC_INSPECT = "ar:i:"  # ar:i:<session.id>
CB_ARC_DELETE = "ar:d:"  # ar:d:<session.id>
CB_ARC_BACK = "ar:back"  # back from inspect to list
CB_ARC_ALL = "ar:all"  # toggle 0-72h vs 0-14d
