// A throwaway program: building it while the image is made fetches every package and plugin a build needs.
package prefetch;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class Prefetch {
    public static void main(String[] args) {
        SpringApplication.run(Prefetch.class, args);
    }
}
