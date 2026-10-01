package com.example.legacy;

import java.io.File;
import java.net.MalformedURLException;
import java.net.URL;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/** Intentionally dated Java 8 code used to exercise migration recipes. */
public class LegacyService {
    public Integer boxedPort(String value) {
        return new Integer(value);
    }

    public URL endpoint(String value) throws MalformedURLException {
        return new URL(value);
    }

    public List<String> activeNames(List<String> names) {
        List<String> result = new ArrayList<String>();
        for (String name : names) {
            if (name != null && name.trim().length() > 0) {
                result.add(name.trim());
            }
        }
        return result;
    }

    public boolean supported(String value) {
        return Arrays.asList("alpha", "beta", "gamma").contains(value);
    }

    public File redundantFile(String path) {
        return new File(new File(path).getAbsolutePath());
    }
}
