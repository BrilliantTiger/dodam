using System.Text;

static class Program
{
    const int MaxInput = 1024 * 1024;

    sealed class Refused : Exception
    {
        public int Code { get; }

        public Refused(int code, string message) : base(message)
        {
            Code = code;
        }
    }

    static bool Inside(string folder, string place)
    {
        return place == folder || place.StartsWith(folder + "/", StringComparison.Ordinal);
    }

    // The place a path really names: every symbolic link on the way is followed.
    static string FollowLinks(string full)
    {
        var parts = full.Split('/', StringSplitOptions.RemoveEmptyEntries).ToList();
        var current = "/";
        var hops = 0;
        for (var i = 0; i < parts.Count; i++)
        {
            var next = Path.Combine(current, parts[i]);
            var info = new FileInfo(next);
            if (info.LinkTarget is string target)
            {
                if (++hops > 40) throw new Refused(2, "too many symbolic links");
                var rest = parts.Skip(i + 1).ToList();
                parts = Path.GetFullPath(target, current).Split('/', StringSplitOptions.RemoveEmptyEntries).ToList();
                parts.AddRange(rest);
                current = "/";
                i = -1;
                continue;
            }
            if (!info.Exists && !Directory.Exists(next)) throw new Refused(2, full + " does not exist");
            current = next;
        }
        return current;
    }

    static string RealPlace(string name)
    {
        return FollowLinks(Path.GetFullPath(name));
    }

    static string Checked(string folder, string name)
    {
        var place = RealPlace(name);
        if (!Inside(folder, place))
        {
            throw new Refused(3, name + " is outside the working folder");
        }
        return place;
    }

    static byte[] ReadLimited(Stream stream)
    {
        var buffer = new byte[MaxInput + 1];
        var size = 0;
        while (size < buffer.Length)
        {
            var got = stream.Read(buffer, size, buffer.Length - size);
            if (got == 0) break;
            size += got;
        }
        return buffer.AsSpan(0, size).ToArray();
    }

    static byte[] ReadInput(string folder, string name)
    {
        byte[] data;
        if (name == "-")
        {
            using var stdin = Console.OpenStandardInput();
            data = ReadLimited(stdin);
        }
        else
        {
            var place = Checked(folder, name);
            if (!File.Exists(place)) throw new Refused(2, name + " is not a file");
            try
            {
                using var file = new FileStream(place, FileMode.Open, FileAccess.Read);
                data = ReadLimited(file);
            }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException)
            {
                throw new Refused(2, name + " cannot be read");
            }
        }
        if (data.Length > MaxInput)
        {
            throw new Refused(2, "the input is larger than 1048576 bytes");
        }
        return data;
    }

    static List<List<string>> Parse(string text)
    {
        var records = new List<List<string>>();
        var record = new List<string>();
        var field = new StringBuilder();
        bool quoted = false, afterQuote = false, atStart = true;
        int n = text.Length, i = 0;
        while (i < n)
        {
            var c = text[i];
            if (quoted)
            {
                if (c == '"')
                {
                    if (i + 1 < n && text[i + 1] == '"')
                    {
                        field.Append('"');
                        i += 2;
                        continue;
                    }
                    quoted = false;
                    afterQuote = true;
                }
                else
                {
                    field.Append(c);
                }
                i += 1;
                continue;
            }
            var lineEnd = c == '\n' ? 1 : c == '\r' && i + 1 < n && text[i + 1] == '\n' ? 2 : 0;
            if (c == ',' || lineEnd > 0)
            {
                record.Add(field.ToString());
                field.Clear();
                atStart = true;
                afterQuote = false;
                if (lineEnd > 0)
                {
                    records.Add(record);
                    record = new List<string>();
                }
                i += lineEnd > 0 ? lineEnd : 1;
                continue;
            }
            if (c == '"')
            {
                if (!atStart) throw new Refused(2, "a double quote where none is allowed");
                quoted = true;
                atStart = false;
            }
            else if (afterQuote)
            {
                throw new Refused(2, "text after a closing quote");
            }
            else
            {
                field.Append(c);
                atStart = false;
            }
            i += 1;
        }
        if (quoted) throw new Refused(2, "a quoted field is not closed");
        if (record.Count > 0 || !atStart)
        {
            record.Add(field.ToString());
            records.Add(record);
        }
        return records;
    }

    static List<List<string>> Table(byte[] data)
    {
        var records = Parse(new UTF8Encoding(false).GetString(data));
        if (records.Count == 0) throw new Refused(2, "the input has no header");
        var header = records[0];
        if (header.Contains("") || header.Distinct(StringComparer.Ordinal).Count() != header.Count)
        {
            throw new Refused(2, "header names must be non-empty and differ");
        }
        foreach (var row in records)
        {
            if (row.Count != header.Count)
            {
                throw new Refused(2, "a record has a different number of fields than the header");
            }
        }
        return records;
    }

    static void Quote(StringBuilder output, string text)
    {
        output.Append('"');
        foreach (var c in text)
        {
            if (c == '"' || c == '\\') output.Append('\\').Append(c);
            else if (c < 0x20) output.Append("\\u").Append(((int)c).ToString("x4"));
            else output.Append(c);
        }
        output.Append('"');
    }

    static byte[] ToJson(List<string> header, IEnumerable<List<string>> rows)
    {
        var output = new StringBuilder("[");
        var first = true;
        foreach (var row in rows)
        {
            output.Append(first ? "{" : ", {");
            first = false;
            for (var i = 0; i < header.Count; i++)
            {
                if (i > 0) output.Append(", ");
                Quote(output, header[i]);
                output.Append(": ");
                Quote(output, row[i]);
            }
            output.Append('}');
        }
        return new UTF8Encoding(false).GetBytes(output.Append("]\n").ToString());
    }

    static string OutputPlace(string folder, string name)
    {
        var parent = Checked(folder, Path.GetDirectoryName(Path.GetFullPath(name)) ?? "/");
        if (!Directory.Exists(parent)) throw new Refused(2, "the folder of " + name + " does not exist");
        return Path.Combine(parent, Path.GetFileName(Path.GetFullPath(name)));
    }

    static bool Exists(string place)
    {
        return File.Exists(place) || Directory.Exists(place) || new FileInfo(place).LinkTarget != null;
    }

    static void WriteNew(string place, byte[] data)
    {
        if (Exists(place)) throw new Refused(3, place + " already exists");
        try
        {
            using var file = new FileStream(place, FileMode.CreateNew, FileAccess.Write);
            file.Write(data, 0, data.Length);
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            throw new Refused(Exists(place) ? 3 : 2, "cannot write " + place);
        }
    }

    static void Run(string[] args)
    {
        if (args.Length != 2) throw new Refused(2, "usage: <input> <output>");
        var folder = RealPlace(Directory.GetCurrentDirectory());
        var place = args[1] == "-" ? null : OutputPlace(folder, args[1]);
        var records = Table(ReadInput(folder, args[0]));
        var data = ToJson(records[0], records.Skip(1));
        if (place == null)
        {
            using var stdout = Console.OpenStandardOutput();
            stdout.Write(data, 0, data.Length);
        }
        else
        {
            WriteNew(place, data);
        }
    }

    static int Main(string[] args)
    {
        try
        {
            Run(args);
            return 0;
        }
        catch (Refused refused)
        {
            Console.Error.WriteLine(refused.Message);
            return refused.Code;
        }
    }
}
