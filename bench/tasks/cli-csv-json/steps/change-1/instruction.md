# Change request: split the output by a column

Keep everything that already works, and add an option that follows the two paths:

    <program> <input> <output> --split <column>

- `<output>` now names a folder that the program creates. It must not exist yet: an existing one
  is refused with exit code 3 and left as it is. `-` is not a folder: exit code 2.
- For every value that `<column>` takes in the records after the header, write the file
  `<output>/<value>.json`: the JSON array of the records with that value, in the order of the
  input, in the same form as before.
- An input with only the header gives an empty folder.
- A `<column>` that is not a header name gives exit code 2.
- The value becomes a file name, so every value of `<column>` must be 1 to 64 characters long and
  made of ASCII letters, digits, `_` and `-` only. When any record breaks this, exit with code 2.
- The rules for paths and for the size of the input hold as before, and whenever the exit code is
  not 0 the folder is not created.
