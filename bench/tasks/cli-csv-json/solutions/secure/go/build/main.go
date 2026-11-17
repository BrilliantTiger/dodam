package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

const maxInput = 1024 * 1024

type refused struct {
	code    int
	message string
}

func (r *refused) Error() string { return r.message }

func refuse(code int, format string, args ...any) error {
	return &refused{code, fmt.Sprintf(format, args...)}
}

func inside(folder, place string) bool {
	return place == folder || strings.HasPrefix(place, folder+string(filepath.Separator))
}

func realPlace(name string) (string, error) {
	place, err := filepath.EvalSymlinks(name)
	if err != nil {
		return "", refuse(2, "%s does not exist", name)
	}
	return filepath.Abs(place)
}

func checked(folder, name string) (string, error) {
	place, err := realPlace(name)
	if err != nil {
		return "", err
	}
	if !inside(folder, place) {
		return "", refuse(3, "%s is outside the working folder", name)
	}
	return place, nil
}

func readLimited(r io.Reader) ([]byte, error) {
	return io.ReadAll(io.LimitReader(r, maxInput+1))
}

func readInput(folder, name string) ([]byte, error) {
	var data []byte
	var err error
	if name == "-" {
		data, err = readLimited(os.Stdin)
	} else {
		var place string
		place, err = checked(folder, name)
		if err != nil {
			return nil, err
		}
		info, statErr := os.Stat(place)
		if statErr != nil || !info.Mode().IsRegular() {
			return nil, refuse(2, "%s is not a file", name)
		}
		file, openErr := os.Open(place)
		if openErr != nil {
			return nil, refuse(2, "%s cannot be read", name)
		}
		defer file.Close()
		data, err = readLimited(file)
	}
	if err != nil {
		return nil, refuse(2, "the input cannot be read")
	}
	if len(data) > maxInput {
		return nil, refuse(2, "the input is larger than 1048576 bytes")
	}
	return data, nil
}

func parse(text []byte) ([][]string, error) {
	var records [][]string
	var record []string
	var field []byte
	quoted, afterQuote, atStart := false, false, true
	n := len(text)
	for i := 0; i < n; {
		c := text[i]
		if quoted {
			if c == '"' {
				if i+1 < n && text[i+1] == '"' {
					field = append(field, '"')
					i += 2
					continue
				}
				quoted, afterQuote = false, true
			} else {
				field = append(field, c)
			}
			i++
			continue
		}
		lineEnd := 0
		if c == '\n' {
			lineEnd = 1
		} else if c == '\r' && i+1 < n && text[i+1] == '\n' {
			lineEnd = 2
		}
		if c == ',' || lineEnd > 0 {
			record = append(record, string(field))
			field = field[:0]
			atStart, afterQuote = true, false
			if lineEnd > 0 {
				records = append(records, record)
				record = nil
				i += lineEnd
			} else {
				i++
			}
			continue
		}
		if c == '"' {
			if !atStart {
				return nil, refuse(2, "a double quote where none is allowed")
			}
			quoted, atStart = true, false
		} else if afterQuote {
			return nil, refuse(2, "text after a closing quote")
		} else {
			field = append(field, c)
			atStart = false
		}
		i++
	}
	if quoted {
		return nil, refuse(2, "a quoted field is not closed")
	}
	if len(record) > 0 || !atStart {
		record = append(record, string(field))
		records = append(records, record)
	}
	return records, nil
}

func table(data []byte) ([]string, [][]string, error) {
	records, err := parse(data)
	if err != nil {
		return nil, nil, err
	}
	if len(records) == 0 {
		return nil, nil, refuse(2, "the input has no header")
	}
	header, rows := records[0], records[1:]
	seen := map[string]bool{}
	for _, name := range header {
		if name == "" || seen[name] {
			return nil, nil, refuse(2, "header names must be non-empty and differ")
		}
		seen[name] = true
	}
	for _, row := range rows {
		if len(row) != len(header) {
			return nil, nil, refuse(2, "a record has a different number of fields than the header")
		}
	}
	return header, rows, nil
}

func toJSON(header []string, rows [][]string) []byte {
	objects := make([]map[string]string, 0, len(rows))
	for _, row := range rows {
		object := make(map[string]string, len(header))
		for i, name := range header {
			object[name] = row[i]
		}
		objects = append(objects, object)
	}
	data, _ := json.Marshal(objects)
	return append(data, '\n')
}

func outputPlace(folder, name string) (string, error) {
	parent, err := checked(folder, filepath.Dir(name))
	if err != nil {
		return "", err
	}
	info, err := os.Stat(parent)
	if err != nil || !info.IsDir() {
		return "", refuse(2, "the folder of %s does not exist", name)
	}
	return filepath.Join(parent, filepath.Base(name)), nil
}

func writeNew(place string, data []byte) error {
	file, err := os.OpenFile(place, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		if errors.Is(err, os.ErrExist) {
			return refuse(3, "%s already exists", place)
		}
		return refuse(2, "cannot write %s", place)
	}
	defer file.Close()
	if _, err := file.Write(data); err != nil {
		return refuse(2, "cannot write %s", place)
	}
	return nil
}

func run(args []string) error {
	if len(args) != 2 {
		return refuse(2, "usage: <input> <output>")
	}
	cwd, err := os.Getwd()
	if err != nil {
		return refuse(2, "no working folder")
	}
	folder, err := realPlace(cwd)
	if err != nil {
		return err
	}
	place := ""
	if args[1] != "-" {
		if place, err = outputPlace(folder, args[1]); err != nil {
			return err
		}
	}
	input, err := readInput(folder, args[0])
	if err != nil {
		return err
	}
	header, rows, err := table(input)
	if err != nil {
		return err
	}
	data := toJSON(header, rows)
	if place == "" {
		_, err = os.Stdout.Write(data)
		return err
	}
	return writeNew(place, data)
}

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		var r *refused
		if errors.As(err, &r) {
			os.Exit(r.code)
		}
		os.Exit(1)
	}
}
