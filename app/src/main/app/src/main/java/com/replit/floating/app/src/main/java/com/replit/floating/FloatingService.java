package com.replit.floating;

import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.PixelFormat;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Message;
import android.util.DisplayMetrics;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.webkit.CookieManager;
import android.webkit.CookieSyncManager;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.RelativeLayout;
import android.widget.SeekBar;
import android.widget.TextView;
import android.widget.Toast;
import android.app.Activity;
import android.content.ClipboardManager;
import android.content.ClipData;
import android.webkit.WebResourceRequest;
import java.util.ArrayList;
import java.util.List;

public class FloatingService extends Service {
    private static FloatingService instance;
    private List<WindowInstance> windowList = new ArrayList<>();

    public static FloatingService getInstance() { return instance; }

    @Override
    public IBinder onBind(Intent intent) { return null; }

    @Override
    public void onCreate() {
        super.onCreate();
        instance = this;

        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.LOLLIPOP) {
            CookieSyncManager.createInstance(this);
        }

        SharedPreferences prefs = getSharedPreferences("FloatingAppPrefs", MODE_PRIVATE);
        String defaultUrl = "https://mainpy-2orgxyfzohrzrytrf9f5sa.streamlit.app/";
        String savedUrl = prefs.getString("custom_url", defaultUrl);

        createNewTab(savedUrl);
    }

    public void createNewTab(String url) {
        WindowInstance newWin = new WindowInstance(this, url);
        newWin.initialize();
        windowList.add(newWin);
    }

    public void closeWindowInstance(WindowInstance win) {
        if (win != null) {
            win.clearAndDestroy();
            windowList.remove(win);
        }
        if (windowList.isEmpty()) { stopSelf(); }
    }

    public void exitAll() {
        for (WindowInstance win : new ArrayList<>(windowList)) { win.clearAndDestroy(); }
        windowList.clear();
        stopSelf();
    }

    @Override
    public void onDestroy() {
        super.onDestroy();
        exitAll();
    }

    public class WindowInstance {
        private Context ctx;
        private String url;
        private WindowManager wm;
        private View view;
        private View layoutFull, layoutBubble, layoutSettings;
        private TextView bubbleView;
        private WebView webView;
        private WindowManager.LayoutParams params;
        private View closeZone;
        private WindowManager.LayoutParams closeParams;
        private boolean isCloseZoneVisible = false;
        private boolean isMaximized = false;
        private boolean wasMaximizedBeforeMinimize = false;
        private View mCustomView;
        private WebChromeClient.CustomViewCallback mCustomViewCallback;
        private boolean wasMaximizedByJS = false;
        private int lastWindowX = 100, lastWindowY = 100;
        private int lastBubbleX = -1, lastBubbleY = -1;

        // File upload support
        private ValueCallback<Uri[]> filePathCallback;
        private static final int FILE_CHOOSER_REQUEST = 1001;

        private TextView btnClose, btnDrag, btnBack, btnRefresh, btnMaximize, btnMinimize, btnSettings;
        private LinearLayout leftPanel;

        private Handler pingHandler = new Handler();
        private Runnable pingRunnable = new Runnable() {
            @Override public void run() { pingHandler.postDelayed(this, 8000); }
        };

        public WindowInstance(Context ctx, String url) {
            this.ctx = ctx;
            this.url = url;
            this.wm = (WindowManager) ctx.getSystemService(Context.WINDOW_SERVICE);
        }

        public void initialize() {
            int layoutId = ctx.getResources().getIdentifier("floating_window", "layout", ctx.getPackageName());
            view = android.view.LayoutInflater.from(ctx).inflate(layoutId, null);

            layoutFull = view.findViewById(getId("layout_full_window"));
            layoutBubble = view.findViewById(getId("layout_bubble"));
            layoutSettings = view.findViewById(getId("layout_settings"));
            bubbleView = (TextView) view.findViewById(getId("bubble_view"));
            webView = (WebView) view.findViewById(getId("webview_replit"));
            leftPanel = (LinearLayout) view.findViewById(getId("left_panel"));

            DisplayMetrics metrics = ctx.getResources().getDisplayMetrics();
            lastBubbleX = metrics.widthPixels - getDp(70);
            lastBubbleY = metrics.heightPixels / 4;

            setupBubbleDesign();
            setupCloseZone();
            setupWindowParams();
            setupControls();
            setupWebView();

            wm.addView(view, params);
            pingHandler.post(pingRunnable);
            loadSavedSettings();

            // Load URL
            webView.loadUrl(url);
        }

        private int getId(String idName) {
            return ctx.getResources().getIdentifier(idName, "id", ctx.getPackageName());
        }

        private void setupBubbleDesign() {
            GradientDrawable shape = new GradientDrawable();
            shape.setShape(GradientDrawable.OVAL);
            shape.setColor(Color.parseColor("#000000"));
            bubbleView.setBackground(shape);

            ViewGroup.LayoutParams lp = bubbleView.getLayoutParams();
            if (lp != null) {
                lp.width = getDp(56);
                lp.height = getDp(56);
                bubbleView.setLayoutParams(lp);
            }
            bubbleView.setGravity(Gravity.CENTER);
            layoutBubble.setAlpha(0.60f);
        }

        private void setupCloseZone() {
            closeZone = new FrameLayout(ctx);
            TextView cross = new TextView(ctx);
            cross.setText("❌");
            cross.setTextSize(26);
            cross.setGravity(Gravity.CENTER);
            GradientDrawable bg = new GradientDrawable();
            bg.setShape(GradientDrawable.OVAL);
            bg.setColor(Color.parseColor("#CCFF3333"));
            closeZone.setBackground(bg);
            ((FrameLayout) closeZone).addView(cross, new FrameLayout.LayoutParams(getDp(65), getDp(65), Gravity.CENTER));

            int FLAG = (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) ?
                    WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY :
                    WindowManager.LayoutParams.TYPE_PHONE;
            closeParams = new WindowManager.LayoutParams(getDp(65), getDp(65), FLAG,
                    WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE, PixelFormat.TRANSLUCENT);
            closeParams.gravity = Gravity.BOTTOM | Gravity.CENTER_HORIZONTAL;
            closeParams.y = getDp(40);
        }

        private void setupWindowParams() {
            int TYPE_FLAG = (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) ?
                    WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY :
                    WindowManager.LayoutParams.TYPE_PHONE;
            int flags = WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL |
                    WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN;
            params = new WindowManager.LayoutParams(getDp(360), getDp(500), TYPE_FLAG, flags, PixelFormat.TRANSLUCENT);
            params.gravity = Gravity.TOP | Gravity.LEFT;
            params.x = lastWindowX;
            params.y = lastWindowY;
        }

        private void updateWindowFocusable(boolean isFullWindow) {
            if (isFullWindow) {
                params.flags = WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL |
                        WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN;
            } else {
                params.flags = WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE |
                        WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL |
                        WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN;
            }
            wm.updateViewLayout(view, params);
        }

        private void applyMenuShift(boolean shiftRight) {
            if (leftPanel == null) return;
            ViewGroup.LayoutParams lp = leftPanel.getLayoutParams();
            if (lp instanceof RelativeLayout.LayoutParams) {
                RelativeLayout.LayoutParams rlp = (RelativeLayout.LayoutParams) lp;
                if (shiftRight) {
                    rlp.removeRule(RelativeLayout.ALIGN_PARENT_LEFT);
                    rlp.addRule(RelativeLayout.ALIGN_PARENT_RIGHT);
                } else {
                    rlp.removeRule(RelativeLayout.ALIGN_PARENT_RIGHT);
                    rlp.addRule(RelativeLayout.ALIGN_PARENT_LEFT);
                }
                leftPanel.setLayoutParams(rlp);
            } else if (lp instanceof FrameLayout.LayoutParams) {
                FrameLayout.LayoutParams flp = (FrameLayout.LayoutParams) lp;
                flp.gravity = shiftRight ? (Gravity.RIGHT | Gravity.CENTER_VERTICAL) : (Gravity.LEFT | Gravity.CENTER_VERTICAL);
                leftPanel.setLayoutParams(flp);
            }
        }

        private void setupControls() {
            btnClose = (TextView) view.findViewById(getId("btn_close"));
            btnDrag = (TextView) view.findViewById(getId("btn_drag"));
            btnBack = (TextView) view.findViewById(getId("btn_back"));
            btnRefresh = (TextView) view.findViewById(getId("btn_refresh"));
            btnMaximize = (TextView) view.findViewById(getId("btn_maximize"));
            btnMinimize = (TextView) view.findViewById(getId("btn_minimize"));
            btnSettings = (TextView) view.findViewById(getId("btn_settings"));

            final EditText inputUrl = (EditText) view.findViewById(getId("input_url"));
            final android.widget.Button btnSaveUrl = (android.widget.Button) view.findViewById(getId("btn_save_url"));
            final SharedPreferences prefs = ctx.getSharedPreferences("FloatingAppPrefs", Context.MODE_PRIVATE);

            String currentSaved = prefs.getString("custom_url", "https://mainpy-2orgxyfzohrzrytrf9f5sa.streamlit.app/");
            inputUrl.setText(currentSaved);

            // Shift Menu Button
            try {
                android.widget.Button btnShiftMenu = new android.widget.Button(ctx);
                btnShiftMenu.setText("↔ Shift Menu Position (L/R)");
                btnShiftMenu.setTextColor(Color.WHITE);
                btnShiftMenu.setBackgroundColor(Color.parseColor("#2962ff"));
                btnShiftMenu.setPadding(10, 10, 10, 10);
                btnShiftMenu.setOnClickListener(new View.OnClickListener() {
                    @Override public void onClick(View v) {
                        boolean isRight = prefs.getBoolean("menu_shift_right", false);
                        boolean newShift = !isRight;
                        prefs.edit().putBoolean("menu_shift_right", newShift).apply();
                        applyMenuShift(newShift);
                        Toast.makeText(ctx, "Menu Position Shifted!", Toast.LENGTH_SHORT).show();
                    }
                });
                if (layoutSettings instanceof ViewGroup) {
                    ((ViewGroup) layoutSettings).addView(btnShiftMenu, 0);
                }
            } catch (Exception e) {}

            // New Tab Button
            android.widget.Button btnAddTab = (android.widget.Button) view.findViewById(getId("btn_add_tab"));
            if (btnAddTab != null) {
                btnAddTab.setOnClickListener(new View.OnClickListener() {
                    @Override public void onClick(View v) {
                        String tabUrl = prefs.getString("custom_url", "https://mainpy-2orgxyfzohrzrytrf9f5sa.streamlit.app/");
                        createNewTab(tabUrl);
                        layoutSettings.setVisibility(View.GONE);
                        Toast.makeText(ctx, "New Tab Opened!", Toast.LENGTH_SHORT).show();
                    }
                });
            }

            btnSaveUrl.setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View v) {
                    String newUrl = inputUrl.getText().toString().trim();
                    if (!newUrl.isEmpty()) {
                        if (!newUrl.startsWith("http://") && !newUrl.startsWith("https://")) {
                            newUrl = "https://" + newUrl;
                        }
                        prefs.edit().putString("custom_url", newUrl).apply();
                        url = newUrl;
                        if (webView != null) { webView.loadUrl(url); }
                        layoutSettings.setVisibility(View.GONE);
                    }
                }
            });

            btnRefresh.setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View v) { if (webView != null) webView.reload(); }
            });

            btnClose.setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View v) { closeWindowInstance(WindowInstance.this); }
            });

            btnDrag.setOnTouchListener(new View.OnTouchListener() {
                int initX, initY; float touchX, touchY;
                @Override public boolean onTouch(View v, MotionEvent e) {
                    if (isMaximized) return false;
                    switch (e.getAction()) {
                        case MotionEvent.ACTION_DOWN:
                            initX = params.x; initY = params.y;
                            touchX = e.getRawX(); touchY = e.getRawY(); return true;
                        case MotionEvent.ACTION_MOVE:
                            params.x = initX + (int)(e.getRawX() - touchX);
                            params.y = initY + (int)(e.getRawY() - touchY);
                            lastWindowX = params.x; lastWindowY = params.y;
                            wm.updateViewLayout(view, params); return true;
                    }
                    return false;
                }
            });

            btnBack.setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View v) { if (webView != null && webView.canGoBack()) webView.goBack(); }
            });

            btnMaximize.setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View v) { toggleMaximize(); }
            });

            btnMinimize.setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View v) { minimizeToBubble(); }
            });

            final TextView btnResizeCorner = (TextView) view.findViewById(getId("btn_resize_corner"));
            btnResizeCorner.setOnTouchListener(new View.OnTouchListener() {
                int initW, initH; float touchX, touchY;
                @Override public boolean onTouch(View v, MotionEvent e) {
                    if (isMaximized) return false;
                    switch (e.getAction()) {
                        case MotionEvent.ACTION_DOWN:
                            initW = params.width; initH = params.height;
                            touchX = e.getRawX(); touchY = e.getRawY(); return true;
                        case MotionEvent.ACTION_MOVE:
                            params.width = Math.max(getDp(200), initW + (int)(e.getRawX() - touchX));
                            params.height = Math.max(getDp(200), initH + (int)(e.getRawY() - touchY));
                            wm.updateViewLayout(view, params); return true;
                    }
                    return false;
                }
            });

            layoutBubble.setOnTouchListener(new View.OnTouchListener() {
                int initX, initY; float touchX, touchY; boolean dragging = false;
                @Override public boolean onTouch(View v, MotionEvent e) {
                    DisplayMetrics metrics = ctx.getResources().getDisplayMetrics();
                    int screenH = metrics.heightPixels; int screenW = metrics.widthPixels;
                    switch (e.getAction()) {
                        case MotionEvent.ACTION_DOWN:
                            initX = params.x; initY = params.y;
                            touchX = e.getRawX(); touchY = e.getRawY();
                            dragging = false; return true;
                        case MotionEvent.ACTION_MOVE:
                            if (Math.abs(e.getRawX() - touchX) > 10 || Math.abs(e.getRawY() - touchY) > 10) {
                                if (!dragging) {
                                    dragging = true;
                                    if (!isCloseZoneVisible) {
                                        try { wm.addView(closeZone, closeParams); isCloseZoneVisible = true; } catch (Exception ex) {}
                                    }
                                }
                                params.x = initX + (int)(e.getRawX() - touchX);
                                params.y = initY + (int)(e.getRawY() - touchY);
                                lastBubbleX = params.x; lastBubbleY = params.y;
                                wm.updateViewLayout(view, params);
                                if (e.getRawY() > screenH - getDp(140) && e.getRawX() > screenW / 3 && e.getRawX() < (screenW * 2) / 3) {
                                    closeZone.setScaleX(1.3f); closeZone.setScaleY(1.3f);
                                } else { closeZone.setScaleX(1.0f); closeZone.setScaleY(1.0f); }
                            } return true;
                        case MotionEvent.ACTION_UP:
                            if (dragging) {
                                if (isCloseZoneVisible) {
                                    try { wm.removeView(closeZone); isCloseZoneVisible = false; } catch (Exception ex) {}
                                }
                                if (e.getRawY() > screenH - getDp(140) && e.getRawX() > screenW / 3 && e.getRawX() < (screenW * 2) / 3) {
                                    closeWindowInstance(WindowInstance.this);
                                }
                            } else { restoreFromBubble(); } return true;
                    }
                    return false;
                }
            });

            btnSettings.setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View v) { layoutSettings.setVisibility(View.VISIBLE); }
            });

            view.findViewById(getId("btn_close_settings")).setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View v) { layoutSettings.setVisibility(View.GONE); }
            });

            // Color buttons
            view.findViewById(getId("color_white")).setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View v) { applyTextColor(Color.WHITE); prefs.edit().putInt("icon_color", Color.WHITE).apply(); }
            });
            view.findViewById(getId("color_red")).setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View v) { applyTextColor(Color.parseColor("#FF5555")); prefs.edit().putInt("icon_color", Color.parseColor("#FF5555")).apply(); }
            });
            view.findViewById(getId("color_green")).setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View v) { applyTextColor(Color.parseColor("#4CAF50")); prefs.edit().putInt("icon_color", Color.parseColor("#4CAF50")).apply(); }
            });
            view.findViewById(getId("color_blue")).setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View v) { applyTextColor(Color.parseColor("#2196
