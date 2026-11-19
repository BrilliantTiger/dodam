# CSV to JSON converter

Build a command-line program that converts one CSV file to JSON:

    <program> <input> <output>

`<input>` names the CSV file to read and `<output>` the JSON file to write. `-` as `<input>` means
standard input, and `-` as `<output>` means standard output. The program is started in a working
folder and takes exactly these two arguments.

## Input

- The input is UTF-8 text. A record ends with a line feed or with a carriage return and a line
  feed; the last record may lack its line end.
- Fields are separated by commas. A field may be wrapped in double quotes. Inside the quotes a
  comma or a line end is part of the field, and two double quotes stand for one. A double quote is
  allowed only as the first character of a quoted field, as its closing quote or doubled inside it,
  and a closing quote is followed by a comma, a line end or the end of the input.
- An empty line is a record with one empty field.
- The first record is the header. Its names are not empty and differ from each other.
- Every later record has exactly as many fields as the header.

## Output

- The output is one JSON array with one object per record after the header, in the order of the
  input. Each object maps every header name to the record's field as a JSON string.
- An input with only the header gives an empty array.
- With `-` as `<output>` the JSON goes to standard output, and nothing else is written there.

## Exit codes

- 0 when the output was written.
- 2 for wrong arguments, an input file that does not exist, an output whose folder does not exist,
  or an input that breaks a rule above.
- 3 when a path is refused by a rule below.

Whenever the exit code is not 0, no output file is created and nothing is written to standard
output. Say what went wrong on standard error.

## Rules

- The program reads and writes only inside its working folder. Follow `..` and symbolic links
  first: when the place a path really names lies outside the working folder, refuse it with exit
  code 3, and read, create or change nothing outside.
- An output that already exists is never overwritten: refuse it with exit code 3 and leave it as
  it is.
- The input is at most 1,048,576 bytes. Refuse a larger input with exit code 2, and never read
  more than that into memory.
