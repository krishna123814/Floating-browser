package com.replit.floating;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;

public class FilePickerActivity extends Activity {
    private static final int FILE_PICK_CODE = 1001;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Intent chooser = getIntent().getParcelableExtra("chooser");
        if (chooser != null) {
            startActivityForResult(chooser, FILE_PICK_CODE);
        } else {
            finish();
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == FILE_PICK_CODE) {
            Uri[] results = null;
            if (resultCode == Activity.RESULT_OK && data != null) {
                if (data.getClipData() != null) {
                    int count = data.getClipData().getItemCount();
                    results = new Uri[count];
                    for (int i = 0; i < count; i++) {
                        results[i] = data.getClipData().getItemAt(i).getUri();
                    }
                } else if (data.getData() != null) {
                    results = new Uri[]{ data.getData() };
                }
            }

            FloatingService service = FloatingService.getInstance();
            if (service != null && !service.getWindowList().isEmpty()) {
                service.getWindowList().get(0).handleFilePickerResult(results);
            }
        }
        finish();
    }
}
