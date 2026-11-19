package app;

import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.channels.FileChannel;
import java.nio.channels.ServerSocketChannel;
import java.nio.channels.SocketChannel;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.core.io.FileSystemResource;
import org.springframework.core.io.Resource;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

/** Copy a file, send and receive it over a socket with the channel's transfer, copy one connection
 * into another and serve it over HTTP, each through the library call a Java program would use. */
@SpringBootApplication
@RestController
public class App {
    public static void main(String[] args) throws Exception {
        switch (args[0]) {
            case "copy" -> Files.copy(Paths.get("in.bin"), Paths.get("out.bin"), StandardCopyOption.REPLACE_EXISTING);
            case "send" -> send();
            case "receive" -> receive();
            case "relay" -> relay();
            default -> {
                System.setProperty("server.port", "18080");
                System.setProperty("server.address", "127.0.0.1");
                System.setProperty("spring.main.banner-mode", "off");
                // a case's program writes in its own folder only: the server's working files go there
                System.setProperty("server.tomcat.basedir", "tomcat");
                System.setProperty("logging.level.root", "ERROR");
                SpringApplication.run(App.class);
                return;
            }
        }
        System.out.println("ok");
    }

    /** The file goes into a socket with the channel's transfer; the other end writes what arrives. */
    static void send() throws Exception {
        ServerSocketChannel server = ServerSocketChannel.open();
        server.bind(new InetSocketAddress("127.0.0.1", 0));
        Thread taking = new Thread(() -> {
            try (SocketChannel c = server.accept(); InputStream in = c.socket().getInputStream()) {
                Files.copy(in, Paths.get("got.bin"), StandardCopyOption.REPLACE_EXISTING);
            } catch (Exception e) {
                throw new RuntimeException(e);
            }
        });
        taking.start();
        try (SocketChannel out = SocketChannel.open(server.getLocalAddress());
             FileChannel in = FileChannel.open(Paths.get("in.bin"))) {
            long at = 0, size = in.size();
            while (at < size) {
                at += in.transferTo(at, size - at, out);
            }
        }
        taking.join();
    }

    /** The file comes out of a socket into a file with the channel's transfer. */
    static void receive() throws Exception {
        ServerSocketChannel server = ServerSocketChannel.open();
        server.bind(new InetSocketAddress("127.0.0.1", 0));
        Thread feeding = new Thread(() -> {
            try (SocketChannel c = SocketChannel.open(server.getLocalAddress()); OutputStream out = c.socket().getOutputStream()) {
                Files.copy(Paths.get("in.bin"), out);
            } catch (Exception e) {
                throw new RuntimeException(e);
            }
        });
        feeding.start();
        try (SocketChannel in = server.accept();
             FileChannel out = FileChannel.open(Paths.get("got.bin"), StandardOpenOption.CREATE, StandardOpenOption.WRITE)) {
            long at = 0, got;
            while ((got = out.transferFrom(in, at, 1 << 20)) > 0) {
                at += got;
            }
        }
        feeding.join();
    }

    /** One connection is copied into another with the stream's own call. */
    static void relay() throws Exception {
        ServerSocket first = new ServerSocket(0, 1, java.net.InetAddress.getLoopbackAddress());
        ServerSocket second = new ServerSocket(0, 1, java.net.InetAddress.getLoopbackAddress());
        Thread feeding = new Thread(() -> {
            try (Socket c = new Socket(first.getInetAddress(), first.getLocalPort())) {
                Files.copy(Paths.get("in.bin"), c.getOutputStream());
            } catch (Exception e) {
                throw new RuntimeException(e);
            }
        });
        Thread taking = new Thread(() -> {
            try (Socket c = second.accept()) {
                Files.copy(c.getInputStream(), Paths.get("got.bin"), StandardCopyOption.REPLACE_EXISTING);
            } catch (Exception e) {
                throw new RuntimeException(e);
            }
        });
        feeding.start();
        taking.start();
        try (Socket in = first.accept(); Socket out = new Socket(second.getInetAddress(), second.getLocalPort())) {
            in.getInputStream().transferTo(out.getOutputStream());
        }
        feeding.join();
        taking.join();
    }

    @GetMapping("/in.bin")
    public ResponseEntity<Resource> file() {
        return ResponseEntity.ok().contentType(MediaType.APPLICATION_OCTET_STREAM).body(new FileSystemResource("in.bin"));
    }

    @GetMapping("/quit")
    public String quit() {
        new Thread(() -> {
            try {
                Thread.sleep(300);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            System.exit(0);
        }).start();
        return "bye";
    }
}
