-- I don't think this does anything right now but at least it's here for
-- git stuff in the future
return {
	"lewis6991/gitsigns.nvim",
	opts = {
		signs = {
			add = { text = "+" },
			change = { text = "~" },
			delete = { text = "_" },
			topdelete = { text = "--" },
			changedelete = { text = "~" },
		},
	},
}
