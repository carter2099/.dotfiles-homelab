return {
	"xiyaowong/transparent.nvim",
	config = function()
		require("transparent").setup({
			exclude_groups = {
				"CursorLine",
				"CursorLineNr",
				"StatusLine",
				"StatusLineNC",
			},
		})
		vim.g.transparent_enabled = true
	end,
}
