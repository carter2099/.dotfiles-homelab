return {
	"nvim-telescope/telescope.nvim",
	tag = "0.1.8",
	-- or                              , branch = '0.1.x',
	dependencies = { "nvim-lua/plenary.nvim" },

	config = function()
		require("telescope").setup({})
		local builtin = require("telescope.builtin")
		local map = vim.keymap
		map.set("n", "<leader>pf", builtin.find_files, {})
		map.set("n", "<leader>ps", function()
			builtin.grep_string({ search = vim.fn.input("Grep > ") })
		end)
		map.set("n", "<leader>vh", builtin.help_tags, {})
	end,
}
