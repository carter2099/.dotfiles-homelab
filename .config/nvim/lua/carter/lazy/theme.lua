return {
	{
		"nordtheme/vim",
		name = "nord",
		config = function()
			--vim.g.nord_cursor_line_number_background = 1
			vim.cmd.colorscheme("nord")
		end,
	},
}
