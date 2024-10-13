-- autocommand group for netrw initialization
vim.api.nvim_create_augroup("netrw_init", { clear = true })
-- autocommand for netrw filetype
-- Fixes issue where lightline does not load on netrw init
vim.api.nvim_create_autocmd("FileType", {
	group = "netrw_init",
	pattern = "netrw",
	callback = function()
		vim.fn["lightline#enable"]()
	end,
})

-- Highlight when yanking text
vim.api.nvim_create_autocmd("TextYankPost", {
	desc = "Highlight when yanking (copying) text",
	group = vim.api.nvim_create_augroup("highlight-yank", { clear = true }),
	callback = function()
		vim.highlight.on_yank()
	end,
})
