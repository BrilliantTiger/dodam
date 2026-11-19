use std::collections::HashSet;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::process;

const MAX_INPUT: u64 = 1024 * 1024;

struct Refused {
    code: i32,
    message: String,
}

fn refuse<T>(code: i32, message: impl Into<String>) -> Result<T, Refused> {
    Err(Refused { code, message: message.into() })
}

fn real_place(name: &Path) -> Result<PathBuf, Refused> {
    fs::canonicalize(name).or_else(|_| refuse(2, format!("{} does not exist", name.display())))
}

fn checked(folder: &Path, name: &Path) -> Result<PathBuf, Refused> {
    let place = real_place(name)?;
    if !place.starts_with(folder) {
        return refuse(3, format!("{} is outside the working folder", name.display()));
    }
    Ok(place)
}

fn read_limited(source: &mut dyn Read) -> io::Result<Vec<u8>> {
    let mut data = Vec::new();
    source.take(MAX_INPUT + 1).read_to_end(&mut data)?;
    Ok(data)
}

fn read_input(folder: &Path, name: &str) -> Result<Vec<u8>, Refused> {
    let data = if name == "-" {
        read_limited(&mut io::stdin().lock())
    } else {
        let place = checked(folder, Path::new(name))?;
        if !place.is_file() {
            return refuse(2, format!("{name} is not a file"));
        }
        File::open(&place).and_then(|mut file| read_limited(&mut file))
    };
    let data = data.or_else(|_| refuse(2, "the input cannot be read"))?;
    if data.len() as u64 > MAX_INPUT {
        return refuse(2, "the input is larger than 1048576 bytes");
    }
    Ok(data)
}

fn text(field: &[u8]) -> String {
    String::from_utf8_lossy(field).into_owned()
}

fn parse(input: &[u8]) -> Result<Vec<Vec<String>>, Refused> {
    let mut records: Vec<Vec<String>> = Vec::new();
    let mut record: Vec<String> = Vec::new();
    let mut field: Vec<u8> = Vec::new();
    let (mut quoted, mut after_quote, mut at_start) = (false, false, true);
    let n = input.len();
    let mut i = 0;
    while i < n {
        let c = input[i];
        if quoted {
            if c == b'"' {
                if i + 1 < n && input[i + 1] == b'"' {
                    field.push(b'"');
                    i += 2;
                    continue;
                }
                quoted = false;
                after_quote = true;
            } else {
                field.push(c);
            }
            i += 1;
            continue;
        }
        let line_end = if c == b'\n' {
            1
        } else if c == b'\r' && i + 1 < n && input[i + 1] == b'\n' {
            2
        } else {
            0
        };
        if c == b',' || line_end > 0 {
            record.push(text(&field));
            field.clear();
            at_start = true;
            after_quote = false;
            if line_end > 0 {
                records.push(std::mem::take(&mut record));
            }
            i += if line_end > 0 { line_end } else { 1 };
            continue;
        }
        if c == b'"' {
            if !at_start {
                return refuse(2, "a double quote where none is allowed");
            }
            quoted = true;
            at_start = false;
        } else if after_quote {
            return refuse(2, "text after a closing quote");
        } else {
            field.push(c);
            at_start = false;
        }
        i += 1;
    }
    if quoted {
        return refuse(2, "a quoted field is not closed");
    }
    if !record.is_empty() || !at_start {
        record.push(text(&field));
        records.push(record);
    }
    Ok(records)
}

fn table(data: &[u8]) -> Result<(Vec<String>, Vec<Vec<String>>), Refused> {
    let mut records = parse(data)?;
    if records.is_empty() {
        return refuse(2, "the input has no header");
    }
    let header = records.remove(0);
    let names: HashSet<&String> = header.iter().collect();
    if header.iter().any(|name| name.is_empty()) || names.len() != header.len() {
        return refuse(2, "header names must be non-empty and differ");
    }
    if records.iter().any(|row| row.len() != header.len()) {
        return refuse(2, "a record has a different number of fields than the header");
    }
    Ok((header, records))
}

fn to_json(header: &[String], rows: &[&Vec<String>]) -> Vec<u8> {
    let objects: Vec<serde_json::Value> = rows
        .iter()
        .map(|row| {
            let mut object = serde_json::Map::new();
            for (name, value) in header.iter().zip(row.iter()) {
                object.insert(name.clone(), serde_json::Value::String(value.clone()));
            }
            serde_json::Value::Object(object)
        })
        .collect();
    let mut data = serde_json::Value::Array(objects).to_string().into_bytes();
    data.push(b'\n');
    data
}

fn output_place(folder: &Path, name: &str) -> Result<PathBuf, Refused> {
    let given = Path::new(name);
    let parent = match given.parent() {
        Some(parent) if !parent.as_os_str().is_empty() => parent,
        _ => Path::new("."),
    };
    let parent = checked(folder, parent)?;
    match given.file_name() {
        Some(file) if parent.is_dir() => Ok(parent.join(file)),
        _ => refuse(2, format!("the folder of {name} does not exist")),
    }
}

fn write_new(place: &Path, data: &[u8]) -> Result<(), Refused> {
    let mut file = match OpenOptions::new().write(true).create_new(true).open(place) {
        Ok(file) => file,
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
            return refuse(3, format!("{} already exists", place.display()));
        }
        Err(_) => return refuse(2, format!("cannot write {}", place.display())),
    };
    file.write_all(data).or_else(|_| refuse(2, format!("cannot write {}", place.display())))
}

fn file_name(value: &str) -> bool {
    (1..=64).contains(&value.len())
        && value.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
}

fn split(folder: &Path, args: &[String]) -> Result<(), Refused> {
    if args[1] == "-" {
        return refuse(2, "--split needs a folder to write to");
    }
    let place = output_place(folder, &args[1])?;
    let (header, rows) = table(&read_input(folder, &args[0])?)?;
    let column = match header.iter().position(|name| name == &args[3]) {
        Some(column) => column,
        None => return refuse(2, format!("no column named {}", args[3])),
    };
    let mut groups: Vec<(&String, Vec<&Vec<String>>)> = Vec::new();
    for row in &rows {
        let value = &row[column];
        if !file_name(value) {
            return refuse(2, "a value of the column cannot be a file name");
        }
        match groups.iter_mut().find(|(known, _)| *known == value) {
            Some((_, group)) => group.push(row),
            None => groups.push((value, vec![row])),
        }
    }
    match fs::create_dir(&place) {
        Ok(()) => {}
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
            return refuse(3, format!("{} already exists", place.display()));
        }
        Err(_) => return refuse(2, format!("cannot create {}", place.display())),
    }
    for (value, group) in &groups {
        write_new(&place.join(format!("{value}.json")), &to_json(&header, group))?;
    }
    Ok(())
}

fn run(args: &[String]) -> Result<(), Refused> {
    let folder = real_place(Path::new("."))?;
    if args.len() == 4 && args[2] == "--split" {
        return split(&folder, args);
    }
    if args.len() != 2 {
        return refuse(2, "usage: <input> <output> [--split <column>]");
    }
    let place = if args[1] == "-" { None } else { Some(output_place(&folder, &args[1])?) };
    let (header, rows) = table(&read_input(&folder, &args[0])?)?;
    let data = to_json(&header, &rows.iter().collect::<Vec<_>>());
    match place {
        None => io::stdout().write_all(&data).or_else(|_| refuse(2, "cannot write the output")),
        Some(place) => write_new(&place, &data),
    }
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if let Err(refused) = run(&args) {
        eprintln!("{}", refused.message);
        process::exit(refused.code);
    }
}
