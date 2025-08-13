# neovim config

A minimal Lazy Neovim setup with vanilla lsp configuration currently supporting Ruby, Go, and Lua. 

I've found that LSP completions get in the way of learning and memorizating syntax and APIs, hindering productivity overall.
Because of that, I've disabled completions for Ruby and Go. Lua completions are still enabled for now.

I prefer coding with just linting, diagnostics, and formatting—on top of an otherwise plain Neovim text-editing environment.

In the future I'll add git integration, and support for Rust once I start learning it.

### Folder Structure
- nvim/
  - init.lua
  - carter/
    - init.lua
    - autocmd/remap/set...
  - config/
    - lazy.lua
    - lsp.lua
      - Contains all lsp configurations
  - plugins/
    - init.lua
      - Register subdirectories (i.e. plugins.colorschemes)
    - colorschemes/
      - Contains all color scheme plugins
    - plugins...

