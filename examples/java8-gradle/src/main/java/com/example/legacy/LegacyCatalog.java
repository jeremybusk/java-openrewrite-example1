package com.example.legacy;

import com.google.common.collect.Lists;

import java.util.Collections;
import java.util.Comparator;
import java.util.List;

/** More intentionally dated Java 8 code for the Gradle migration path. */
public class LegacyCatalog {
    public List<String> sorted(String... values) {
        List<String> result = Lists.newArrayList(values);
        Collections.sort(result, new Comparator<String>() {
            @Override
            public int compare(String left, String right) {
                return left.compareTo(right);
            }
        });
        return result;
    }

    public String label(boolean active) {
        if (active == true) {
            return "active";
        } else {
            return "inactive";
        }
    }
}
