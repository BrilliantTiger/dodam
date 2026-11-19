package app;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.FileAlreadyExistsException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public class App {
    static final int MAX_INPUT = 1024 * 1024;

    static class Refused extends Exception {
        final int code;

        Refused(int code, String message) {
            super(message);
            this.code = code;
        }
    }

    static Path realPlace(String name) throws Refused {
        try {
            return Path.of(name).toRealPath();
        } catch (IOException | RuntimeException e) {
            throw new Refused(2, name + " does not exist");
        }
    }

    static Path checked(Path folder, String name) throws Refused {
        Path place = realPlace(name);
        if (!place.startsWith(folder)) {
            throw new Refused(3, name + " is outside the working folder");
        }
        return place;
    }

    static byte[] readLimited(InputStream in) throws IOException {
        return in.readNBytes(MAX_INPUT + 1);
    }

    static byte[] readInput(Path folder, String name) throws Refused {
        byte[] data;
        try {
            if (name.equals("-")) {
                data = readLimited(System.in);
            } else {
                Path place = checked(folder, name);
                if (!Files.isRegularFile(place)) {
                    throw new Refused(2, name + " is not a file");
                }
                try (InputStream in = Files.newInputStream(place)) {
                    data = readLimited(in);
                }
            }
        } catch (IOException e) {
            throw new Refused(2, "the input cannot be read");
        }
        if (data.length > MAX_INPUT) {
            throw new Refused(2, "the input is larger than 1048576 bytes");
        }
        return data;
    }

    static List<List<String>> parse(String text) throws Refused {
        List<List<String>> records = new ArrayList<>();
        List<String> record = new ArrayList<>();
        StringBuilder field = new StringBuilder();
        boolean quoted = false;
        boolean afterQuote = false;
        boolean atStart = true;
        int n = text.length();
        int i = 0;
        while (i < n) {
            char c = text.charAt(i);
            if (quoted) {
                if (c == '"') {
                    if (i + 1 < n && text.charAt(i + 1) == '"') {
                        field.append('"');
                        i += 2;
                        continue;
                    }
                    quoted = false;
                    afterQuote = true;
                } else {
                    field.append(c);
                }
                i += 1;
                continue;
            }
            int lineEnd = c == '\n' ? 1 : c == '\r' && i + 1 < n && text.charAt(i + 1) == '\n' ? 2 : 0;
            if (c == ',' || lineEnd > 0) {
                record.add(field.toString());
                field.setLength(0);
                atStart = true;
                afterQuote = false;
                if (lineEnd > 0) {
                    records.add(record);
                    record = new ArrayList<>();
                }
                i += lineEnd > 0 ? lineEnd : 1;
                continue;
            }
            if (c == '"') {
                if (!atStart) {
                    throw new Refused(2, "a double quote where none is allowed");
                }
                quoted = true;
                atStart = false;
            } else if (afterQuote) {
                throw new Refused(2, "text after a closing quote");
            } else {
                field.append(c);
                atStart = false;
            }
            i += 1;
        }
        if (quoted) {
            throw new Refused(2, "a quoted field is not closed");
        }
        if (!record.isEmpty() || !atStart) {
            record.add(field.toString());
            records.add(record);
        }
        return records;
    }

    static List<List<String>> table(byte[] data) throws Refused {
        List<List<String>> records = parse(new String(data, StandardCharsets.UTF_8));
        if (records.isEmpty()) {
            throw new Refused(2, "the input has no header");
        }
        List<String> header = records.get(0);
        if (header.contains("") || new HashSet<>(header).size() != header.size()) {
            throw new Refused(2, "header names must be non-empty and differ");
        }
        for (List<String> row : records) {
            if (row.size() != header.size()) {
                throw new Refused(2, "a record has a different number of fields than the header");
            }
        }
        return records;
    }

    static void quote(StringBuilder out, String text) {
        out.append('"');
        for (int i = 0; i < text.length(); i++) {
            char c = text.charAt(i);
            if (c == '"' || c == '\\') {
                out.append('\\').append(c);
            } else if (c < 0x20) {
                out.append(String.format("\\u%04x", (int) c));
            } else {
                out.append(c);
            }
        }
        out.append('"');
    }

    static byte[] toJson(List<String> header, List<List<String>> rows) {
        StringBuilder out = new StringBuilder("[");
        for (int r = 0; r < rows.size(); r++) {
            out.append(r == 0 ? "{" : ", {");
            for (int i = 0; i < header.size(); i++) {
                if (i > 0) {
                    out.append(", ");
                }
                quote(out, header.get(i));
                out.append(": ");
                quote(out, rows.get(r).get(i));
            }
            out.append("}");
        }
        return out.append("]\n").toString().getBytes(StandardCharsets.UTF_8);
    }

    static Path outputPlace(Path folder, String name) throws Refused {
        Path given = Path.of(name);
        Path parent = checked(folder, given.getParent() == null ? "." : given.getParent().toString());
        if (!Files.isDirectory(parent) || given.getFileName() == null) {
            throw new Refused(2, "the folder of " + name + " does not exist");
        }
        return parent.resolve(given.getFileName());
    }

    static void writeNew(Path place, byte[] data) throws Refused {
        try {
            Files.write(place, data, StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE);
        } catch (FileAlreadyExistsException e) {
            throw new Refused(3, place + " already exists");
        } catch (IOException e) {
            throw new Refused(2, "cannot write " + place);
        }
    }

    static void split(Path folder, String[] args) throws Refused {
        if (args[1].equals("-")) {
            throw new Refused(2, "--split needs a folder to write to");
        }
        Path place = outputPlace(folder, args[1]);
        List<List<String>> records = table(readInput(folder, args[0]));
        List<String> header = records.get(0);
        int column = header.indexOf(args[3]);
        if (column < 0) {
            throw new Refused(2, "no column named " + args[3]);
        }
        Map<String, List<List<String>>> groups = new LinkedHashMap<>();
        for (List<String> row : records.subList(1, records.size())) {
            if (!row.get(column).matches("[A-Za-z0-9_-]{1,64}")) {
                throw new Refused(2, "a value of the column cannot be a file name");
            }
            groups.computeIfAbsent(row.get(column), value -> new ArrayList<>()).add(row);
        }
        try {
            Files.createDirectory(place);
        } catch (FileAlreadyExistsException e) {
            throw new Refused(3, place + " already exists");
        } catch (IOException e) {
            throw new Refused(2, "cannot create " + place);
        }
        for (Map.Entry<String, List<List<String>>> group : groups.entrySet()) {
            writeNew(place.resolve(group.getKey() + ".json"), toJson(header, group.getValue()));
        }
    }

    static void run(String[] args) throws Refused {
        Path folder = realPlace(".");
        if (args.length == 4 && args[2].equals("--split")) {
            split(folder, args);
            return;
        }
        if (args.length != 2) {
            throw new Refused(2, "usage: <input> <output> [--split <column>]");
        }
        Path place = args[1].equals("-") ? null : outputPlace(folder, args[1]);
        List<List<String>> records = table(readInput(folder, args[0]));
        byte[] data = toJson(records.get(0), records.subList(1, records.size()));
        if (place == null) {
            System.out.write(data, 0, data.length);
            System.out.flush();
        } else {
            writeNew(place, data);
        }
    }

    public static void main(String[] args) {
        try {
            run(args);
        } catch (Refused refused) {
            System.err.println(refused.getMessage());
            System.exit(refused.code);
        }
    }
}
