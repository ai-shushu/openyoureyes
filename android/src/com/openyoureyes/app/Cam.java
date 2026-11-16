package com.openyoureyes.app;

import android.annotation.SuppressLint;
import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.ImageFormat;
import android.graphics.Matrix;
import android.graphics.Rect;
import android.graphics.YuvImage;
import android.hardware.camera2.CameraAccessException;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CaptureRequest;
import android.hardware.camera2.TotalCaptureResult;
import android.hardware.camera2.params.StreamConfigurationMap;
import android.media.Image;
import android.media.ImageReader;
import android.os.Handler;
import android.os.HandlerThread;
import android.util.Size;
import android.view.Surface;

import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.util.Arrays;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/**
 * 摄像头（Camera2，只用后置）。按需打开、用完就关，平时不占摄像头、不耗电。
 *
 * 一次「会话」= 打开摄像头 + 一路小分辨率 YUV 预览（测光、自动曝光收敛、取景画面）+ 可选一路全尺寸 JPEG（拍照）。
 * 所有方法都是阻塞的，由 EyesService 的工作线程调用；同一时间只会有一个会话。
 */
final class Cam {
    interface FrameSink {
        /** 每来一帧预览调用一次（已转成竖向的 JPEG）。返回 false 结束取景。 */
        boolean onFrame(byte[] jpeg);
    }

    private final Context ctx;
    private final CameraManager mgr;
    private final HandlerThread thread;
    private final Handler handler;
    private String camId;
    private int sensorOrientation = 90;
    private boolean hasFlash;
    private Size jpegSize, smallSize, liveLow, liveHigh;

    private CameraDevice dev;
    private CameraCaptureSession session;
    private ImageReader yuvReader, jpegReader;
    private CaptureRequest.Builder previewReq;
    private volatile int lastLuma = -1;
    private final AtomicReference<byte[]> latestYuv = new AtomicReference<>();
    private int yuvW, yuvH;

    // 实时取景时直接拍（不关摄像头、不重开）：拍照线程放一个请求，取景线程在自己的会话里拍好交回
    private volatile boolean liveCanShoot;
    private final Object shotLock = new Object();
    private volatile boolean shotPending;
    private String shotLight;
    private byte[] shotOut;
    private Exception shotErr;
    private CountDownLatch shotDone;

    Cam(Context c) {
        ctx = c.getApplicationContext();
        mgr = (CameraManager) ctx.getSystemService(Context.CAMERA_SERVICE);
        thread = new HandlerThread("cam");
        thread.start();
        handler = new Handler(thread.getLooper());
        try {
            for (String id : mgr.getCameraIdList()) {
                CameraCharacteristics ch = mgr.getCameraCharacteristics(id);
                Integer facing = ch.get(CameraCharacteristics.LENS_FACING);
                if (facing != null && facing == CameraCharacteristics.LENS_FACING_BACK) {
                    camId = id;
                    Integer so = ch.get(CameraCharacteristics.SENSOR_ORIENTATION);
                    sensorOrientation = so == null ? 90 : so;
                    Boolean fl = ch.get(CameraCharacteristics.FLASH_INFO_AVAILABLE);
                    hasFlash = fl != null && fl;
                    StreamConfigurationMap map = ch.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
                    Size[] j = map.getOutputSizes(ImageFormat.JPEG);
                    Size[] y = map.getOutputSizes(ImageFormat.YUV_420_888);
                    jpegSize = largest(j);
                    smallSize = closest(y, 640, 480);
                    liveLow = closest(y, 854, 480);
                    liveHigh = closest(y, 1280, 720);
                    break;
                }
            }
        } catch (Exception ignored) {
        }
    }

    boolean ok() { return camId != null; }
    boolean hasFlash() { return hasFlash; }
    int lastLuma() { return lastLuma; }
    int sensorOrientation() { return sensorOrientation; }

    private static Size largest(Size[] s) {
        Size best = s[0];
        for (Size x : s) if ((long) x.getWidth() * x.getHeight() > (long) best.getWidth() * best.getHeight()) best = x;
        return best;
    }

    private static Size closest(Size[] s, int w, int h) {
        Size best = s[0];
        long bestD = Long.MAX_VALUE;
        for (Size x : s) {
            long d = Math.abs((long) x.getWidth() * x.getHeight() - (long) w * h) + Math.abs(x.getWidth() * 3 - x.getHeight() * 4) * 10L;
            if (x.getWidth() * 9 == x.getHeight() * 16 && w * 9 == h * 16) d /= 2;
            if (d < bestD) { bestD = d; best = x; }
        }
        return best;
    }

    // ------------------------------------------------------------------ 会话

    @SuppressLint("MissingPermission")
    private void open(Size yuv, boolean withJpeg) throws Exception {
        close();
        yuvW = yuv.getWidth();
        yuvH = yuv.getHeight();
        yuvReader = ImageReader.newInstance(yuvW, yuvH, ImageFormat.YUV_420_888, 3);
        yuvReader.setOnImageAvailableListener(new ImageReader.OnImageAvailableListener() {
            public void onImageAvailable(ImageReader r) {
                Image im = r.acquireLatestImage();
                if (im == null) return;
                try {
                    byte[] nv21 = toNv21(im);
                    latestYuv.set(nv21);
                    lastLuma = luma(nv21, yuvW, yuvH);
                } finally {
                    im.close();
                }
            }
        }, handler);
        if (withJpeg) jpegReader = ImageReader.newInstance(jpegSize.getWidth(), jpegSize.getHeight(), ImageFormat.JPEG, 2);

        final CountDownLatch opened = new CountDownLatch(1);
        final Exception[] err = new Exception[1];
        mgr.openCamera(camId, new CameraDevice.StateCallback() {
            public void onOpened(CameraDevice d) { dev = d; opened.countDown(); }
            public void onDisconnected(CameraDevice d) { d.close(); err[0] = new Exception("摄像头被其它应用占用"); opened.countDown(); }
            public void onError(CameraDevice d, int e) { d.close(); err[0] = new Exception("摄像头打不开（" + e + "）"); opened.countDown(); }
        }, handler);
        if (!opened.await(6, TimeUnit.SECONDS)) throw new Exception("摄像头打开超时");
        if (err[0] != null) throw err[0];

        final CountDownLatch configured = new CountDownLatch(1);
        Surface ys = yuvReader.getSurface();
        java.util.List<Surface> outs = withJpeg ? Arrays.asList(ys, jpegReader.getSurface()) : Arrays.asList(ys);
        dev.createCaptureSession(outs, new CameraCaptureSession.StateCallback() {
            public void onConfigured(CameraCaptureSession s) { session = s; configured.countDown(); }
            public void onConfigureFailed(CameraCaptureSession s) { configured.countDown(); }
        }, handler);
        if (!configured.await(6, TimeUnit.SECONDS) || session == null) throw new Exception("摄像头配置失败");

        previewReq = dev.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW);
        previewReq.addTarget(ys);
        previewReq.set(CaptureRequest.CONTROL_MODE, CaptureRequest.CONTROL_MODE_AUTO);
        previewReq.set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE);
        previewReq.set(CaptureRequest.CONTROL_AE_MODE, CaptureRequest.CONTROL_AE_MODE_ON);
        session.setRepeatingRequest(previewReq.build(), null, handler);
    }

    void close() {
        try { if (session != null) session.close(); } catch (Exception ignored) { }
        try { if (dev != null) dev.close(); } catch (Exception ignored) { }
        try { if (yuvReader != null) yuvReader.close(); } catch (Exception ignored) { }
        try { if (jpegReader != null) jpegReader.close(); } catch (Exception ignored) { }
        session = null; dev = null; yuvReader = null; jpegReader = null;
        latestYuv.set(null);
    }

    private void setTorch(boolean on) throws CameraAccessException {
        if (!hasFlash || previewReq == null) return;
        previewReq.set(CaptureRequest.FLASH_MODE, on ? CaptureRequest.FLASH_MODE_TORCH : CaptureRequest.FLASH_MODE_OFF);
        session.setRepeatingRequest(previewReq.build(), null, handler);
    }

    /** 等预览出画面、自动曝光大致稳定。 */
    private void settle(long ms) throws InterruptedException {
        long end = System.currentTimeMillis() + ms;
        while (latestYuv.get() == null && System.currentTimeMillis() < end + 2000) Thread.sleep(50);
        long left = end - System.currentTimeMillis();
        if (left > 0) Thread.sleep(left);
    }

    // ------------------------------------------------------------------ 对外功能

    /**
     * 拍一张全尺寸照片。light：on 补光、off 不补光、auto 太暗才补（null 当 auto）。
     * 返回 JPEG（已按传感器方向写好 EXIF 方向，竖向显示）。
     */
    synchronized byte[] photo(String light) throws Exception {
        open(smallSize, true);
        try {
            settle(900);
            return still(light);
        } finally {
            close();
        }
    }

    /** 在已经开着、带拍照输出的会话里拍一张全尺寸照片（补光用完就关回去）。 */
    private byte[] still(String light) throws Exception {
        boolean torch = "on".equals(light) || (!"off".equals(light) && hasFlash && lastLuma >= 0 && lastLuma < 55);
        if (torch) { setTorch(true); settle(1200); }
        try {
            final AtomicReference<byte[]> out = new AtomicReference<>();
            final CountDownLatch got = new CountDownLatch(1);
            jpegReader.setOnImageAvailableListener(new ImageReader.OnImageAvailableListener() {
                public void onImageAvailable(ImageReader r) {
                    Image im = r.acquireLatestImage();
                    if (im == null) return;
                    try {
                        ByteBuffer b = im.getPlanes()[0].getBuffer();
                        byte[] d = new byte[b.remaining()];
                        b.get(d);
                        out.set(d);
                    } finally {
                        im.close();
                        got.countDown();
                    }
                }
            }, handler);
            CaptureRequest.Builder still = dev.createCaptureRequest(CameraDevice.TEMPLATE_STILL_CAPTURE);
            still.addTarget(jpegReader.getSurface());
            still.set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE);
            still.set(CaptureRequest.CONTROL_AE_MODE, CaptureRequest.CONTROL_AE_MODE_ON);
            still.set(CaptureRequest.JPEG_ORIENTATION, sensorOrientation);
            still.set(CaptureRequest.JPEG_QUALITY, (byte) 92);
            if (torch) still.set(CaptureRequest.FLASH_MODE, CaptureRequest.FLASH_MODE_TORCH);
            session.capture(still.build(), new CameraCaptureSession.CaptureCallback() {
                public void onCaptureCompleted(CameraCaptureSession s, CaptureRequest r, TotalCaptureResult res) { }
            }, handler);
            if (!got.await(8, TimeUnit.SECONDS) || out.get() == null) throw new Exception("拍照超时");
            return out.get();
        } finally {
            if (torch) try { setTorch(false); } catch (Exception ignored) { }
        }
    }

    /** 实时取景开着、而且能直接拍吗。 */
    boolean canShootLive() { return liveCanShoot; }

    /** 实时取景开着时直接拍一张（按下就拍，不停画面）。拍不了抛异常，调用方改用 photo()。 */
    byte[] shootLive(String light, long timeoutMs) throws Exception {
        CountDownLatch d = new CountDownLatch(1);
        synchronized (shotLock) {
            if (!liveCanShoot) throw new Exception("实时画面没开");
            shotLight = light; shotOut = null; shotErr = null; shotDone = d; shotPending = true;
        }
        if (!d.await(timeoutMs, TimeUnit.MILLISECONDS)) {
            synchronized (shotLock) { shotPending = false; }
            throw new Exception("拍照超时");
        }
        synchronized (shotLock) {
            if (shotErr != null) throw shotErr;
            return shotOut;
        }
    }

    /** 取景线程里调用：有人要拍就在当前会话里拍。 */
    private void servePendingShot() {
        String light;
        CountDownLatch d;
        synchronized (shotLock) {
            if (!shotPending) return;
            shotPending = false;
            light = shotLight; d = shotDone;
        }
        byte[] out = null;
        Exception err = null;
        try { out = still(light); } catch (Exception e) { err = e; }
        synchronized (shotLock) { shotOut = out; shotErr = err; }
        d.countDown();
    }

    private void failPendingShot(String why) {
        synchronized (shotLock) {
            if (!shotPending) return;
            shotPending = false;
            shotErr = new Exception(why);
            shotDone.countDown();
        }
    }

    /** 一张小图（长边 maxSide），给封面用。 */
    synchronized byte[] small(int maxSide) throws Exception {
        open(smallSize, false);
        try {
            settle(1200);
            byte[] nv21 = latestYuv.get();
            if (nv21 == null) throw new Exception("没有画面");
            return yuvToJpeg(nv21, yuvW, yuvH, maxSide, 60);
        } finally {
            close();
        }
    }

    /** 实时取景：持续把预览画面转成 JPEG 交给 sink，直到 sink 返回 false。 */
    synchronized void live(boolean high, int fps, FrameSink sink) throws Exception {
        try {
            open(high ? liveHigh : liveLow, true);   // 带上拍照输出：按快门时直接在这一路拍
            liveCanShoot = true;
        } catch (Exception e) {   // 有的旧手机不支持同时开两路：只开画面，拍照时再重开
            open(high ? liveHigh : liveLow, false);
        }
        try {
            long interval = 1000L / Math.max(1, fps);
            byte[] last = null;
            while (true) {
                long t0 = System.currentTimeMillis();
                if (shotPending) servePendingShot();
                byte[] nv21 = latestYuv.get();
                if (nv21 != null && nv21 != last) {
                    last = nv21;
                    if (!sink.onFrame(yuvToJpeg(nv21, yuvW, yuvH, 0, high ? 70 : 60))) break;
                } else if (!sink.onFrame(null)) {
                    break;
                }
                long wait = interval - (System.currentTimeMillis() - t0);
                while (wait > 0 && !shotPending) {   // 等下一帧时有人按快门：马上拍
                    Thread.sleep(Math.min(wait, 20));
                    wait = interval - (System.currentTimeMillis() - t0);
                }
            }
        } finally {
            liveCanShoot = false;
            failPendingShot("实时画面停了");
            close();
        }
    }

    // ------------------------------------------------------------------ 图像小工具

    private static byte[] toNv21(Image im) {
        int w = im.getWidth(), h = im.getHeight();
        Image.Plane[] p = im.getPlanes();
        byte[] out = new byte[w * h * 3 / 2];
        ByteBuffer yb = p[0].getBuffer();
        int yRow = p[0].getRowStride();
        int pos = 0;
        for (int r = 0; r < h; r++) {
            yb.position(r * yRow);
            yb.get(out, pos, w);
            pos += w;
        }
        ByteBuffer ub = p[1].getBuffer(), vb = p[2].getBuffer();
        int uvRow = p[1].getRowStride(), uvPix = p[1].getPixelStride();
        for (int r = 0; r < h / 2; r++) {
            for (int c = 0; c < w / 2; c++) {
                int i = r * uvRow + c * uvPix;
                out[pos++] = vb.get(i);
                out[pos++] = ub.get(i);
            }
        }
        return out;
    }

    private static int luma(byte[] nv21, int w, int h) {
        long sum = 0;
        int n = 0;
        for (int y = 0; y < h; y += 8) for (int x = 0; x < w; x += 8) { sum += nv21[y * w + x] & 0xff; n++; }
        return n == 0 ? -1 : (int) (sum / n);
    }

    /** NV21 → 竖向 JPEG（按传感器方向旋转），maxSide>0 时缩小到长边 maxSide。 */
    private byte[] yuvToJpeg(byte[] nv21, int w, int h, int maxSide, int quality) {
        ByteArrayOutputStream o = new ByteArrayOutputStream();
        new YuvImage(nv21, ImageFormat.NV21, w, h, null).compressToJpeg(new Rect(0, 0, w, h), 85, o);
        int rot = sensorOrientation % 360;
        if (rot == 0 && maxSide <= 0) return o.toByteArray();
        Bitmap b = BitmapFactory.decodeByteArray(o.toByteArray(), 0, o.size());
        Matrix m = new Matrix();
        if (rot != 0) m.postRotate(rot);
        if (maxSide > 0) {
            float k = Math.min(1f, maxSide / (float) Math.max(w, h));
            m.postScale(k, k);
        }
        Bitmap r = Bitmap.createBitmap(b, 0, 0, b.getWidth(), b.getHeight(), m, true);
        ByteArrayOutputStream o2 = new ByteArrayOutputStream();
        r.compress(Bitmap.CompressFormat.JPEG, quality, o2);
        b.recycle();
        if (r != b) r.recycle();
        return o2.toByteArray();
    }
}
