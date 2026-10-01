package com.example.legacy;

import org.junit.Test;

import java.util.Arrays;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

public class LegacyServiceTest {
    @Test
    public void filtersNames() {
        LegacyService service = new LegacyService();
        assertEquals(Arrays.asList("Ada", "Grace"),
                service.activeNames(Arrays.asList(" Ada ", null, "", "Grace")));
        assertTrue(service.supported("alpha"));
    }
}
