# Third-Party Notices

This project (licensed under the MIT License) uses the following third-party
components. Their licenses are reproduced/acknowledged here for attribution.

## markdown-it

- **Component:** markdown-it — Markdown parser, used to render assistant answers
- **Usage:** bundled in the frontend as `frontend/static/markdown-it.min.js`
  (unmodified) and served to the browser; the file retains its own license header
- **Homepage / Source:** <https://github.com/markdown-it/markdown-it>
- **License:** MIT — <https://github.com/markdown-it/markdown-it/blob/master/LICENSE>

Copyright (c) 2014 Vitaly Puzrin, Alex Kocharin

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
the Software, and to permit persons to whom the Software is furnished to do so,
subject to the inclusion of the above copyright notice and this permission
notice in all copies or substantial portions of the Software.

## PyYAML

- **Component:** PyYAML — YAML parser and emitter, used to load `config.yml`
  (application configuration) in the agent and indexer services
- **Usage:** installed as a Python dependency (`pydantic-settings[yaml]`,
  `bedtimenews-indexer`)
- **Homepage / Source:** <https://pyyaml.org/>
- **License:** MIT — <https://github.com/yaml/pyyaml/blob/main/LICENSE>

Copyright (c) 2017-2021 Ingy döt Net
Copyright (c) 2006-2016 Kirill Simonov

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to do
so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
