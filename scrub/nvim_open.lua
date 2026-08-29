-- Show one scrub handoff inside a running nvim.
--
-- The bridge rewrites this file with the current request substituted in, then
-- executes it over RPC. It keeps a single tab for scrub and reuses it, so a
-- session of scrubbing leaves one diff tab behind rather than thirty, and the
-- rest of the user's layout is never touched.

local request = __SCRUB_REQUEST__

local function readonly()
  vim.cmd("setlocal readonly nomodifiable")
end

--- Colour the + and - lines of a unified diff.
--- Set rather than detected: filetype detection can be off or overridden, and
--- an uncoloured diff is the one thing this view exists to provide.
local function set_filetype(path)
  if path:sub(-5) == ".diff" then
    vim.cmd("setlocal filetype=diff")
  end
end

local function claim_tab()
  local tab = vim.g.scrub_tab
  if tab and vim.api.nvim_tabpage_is_valid(tab) then
    vim.api.nvim_set_current_tabpage(tab)
    -- Collapse last handoff's diff split; `only` is scoped to this tab, so
    -- windows elsewhere in the user's layout survive.
    vim.cmd("silent! only!")
    vim.cmd("silent! diffoff!")
    return tab
  end
  vim.cmd("tabnew")
  tab = vim.api.nvim_get_current_tabpage()
  vim.g.scrub_tab = tab
  return tab
end

-- Run on nvim's own event loop rather than inside the RPC call.
--
-- `--remote-expr` executes on the main loop and blocks nvim until it returns.
-- Anything in here that can prompt -- a modified buffer, a slow LSP attach, a
-- plugin autocmd -- would therefore freeze the editor with no way to answer,
-- which looks exactly like a hang. Scheduling hands the work back to nvim, so
-- prompts stay answerable and a slow plugin costs latency rather than a lock-up.
--
-- pcall so a failure surfaces as a message instead of leaving a half-built
-- layout behind.
vim.schedule(function()
  local ok, err = pcall(function()
  local caller = vim.api.nvim_get_current_win()

  claim_tab()
  vim.cmd("silent! edit! " .. vim.fn.fnameescape(request.left))
  readonly()
  set_filetype(request.left)

  -- Land on the region the user selected, centred, rather than line 1.
  if request.line then
    pcall(vim.api.nvim_win_set_cursor, 0, { request.line, 0 })
    vim.cmd("normal! zz")
  end

  if request.right then
    local old = vim.api.nvim_get_current_win()
    vim.cmd("vertical rightbelow diffsplit " .. vim.fn.fnameescape(request.right))
    readonly()

    -- Colour only the new side. nvim's diff highlighting is symmetric and
    -- relational, so the old pane paints DiffAdd (green) on lines the commit
    -- *removed* -- the exact inverse of what green means everywhere else. Two
    -- panes of contradicting colour is harder to read than one, so the old side
    -- is neutralised into plain reference text. winhighlight is window-local, so
    -- diff mode itself is untouched: alignment, scrollbind and folding all stay.
    vim.wo[old].winhighlight = table.concat({
      "DiffAdd:Normal",
      "DiffChange:Normal",
      "DiffText:Normal",
      "DiffDelete:NonText",
    }, ",")
  end

  -- When scrub runs inside nvim's own :terminal the RPC steals the cursor from
  -- the scrubber; hand it back and resume terminal mode so arrow keys keep
  -- driving the playhead. In a separate terminal pane the caller is not a
  -- terminal buffer and this is a no-op.
  vim.schedule(function()
    if not vim.api.nvim_win_is_valid(caller) then
      return
    end
    local buf = vim.api.nvim_win_get_buf(caller)
    if vim.api.nvim_get_option_value("buftype", { buf = buf }) == "terminal" then
      vim.api.nvim_set_current_win(caller)
      vim.cmd("startinsert")
    end
  end)

  end)
  if not ok then
    vim.notify("scrub: " .. tostring(err), vim.log.levels.WARN)
  end
end)

return 1
